"""
Declaration of `Configuration` class.
"""

import platform
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union, get_type_hints

import numpy as np

# pylint: disable=import-error
from mlir._mlir_libs._concretelang._compiler import KeysetRestriction, RangeRestriction

from ..dtypes import Integer
from ..representation import GraphProcessor
from ..values import ValueDescription
from .utils import friendly_type_format

# pylint: enable=import-error


MAXIMUM_TLU_BIT_WIDTH = 16

DEFAULT_P_ERROR = None
DEFAULT_GLOBAL_P_ERROR = 1 / 100_000


class SecurityLevel(int, Enum):
    """
    Security level used to optimize the circuit parameters.
    """

    SECURITY_128_BITS = 128
    SECURITY_132_BITS = 132


class ParameterSelectionStrategy(str, Enum):
    """
    ParameterSelectionStrategy, to set optimization strategy.
    """

    V0 = "v0"
    MONO = "mono"
    MULTI = "multi"

    @classmethod
    def parse(cls, string: str) -> "ParameterSelectionStrategy":
        """Convert a string to a ParameterSelectionStrategy."""
        if isinstance(string, cls):
            return string
        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)
        for value in ParameterSelectionStrategy:
            if string.lower() == value.value:
                return value
        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in ParameterSelectionStrategy)})"
        )
        raise ValueError(message)


class MultiParameterStrategy(str, Enum):
    """
    MultiParamStrategy, to set optimization strategy for multi-parameter.
    """

    PRECISION = "precision"
    PRECISION_AND_NORM2 = "precision_and_norm2"

    @classmethod
    def parse(cls, string: str) -> "MultiParameterStrategy":
        """Convert a string to a MultiParamStrategy."""
        if isinstance(string, cls):
            return string
        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)
        for value in MultiParameterStrategy:  # pragma: no cover
            if string.lower().replace("-", "_") == value.value:
                return value
        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in MultiParameterStrategy)})"
        )
        raise ValueError(message)


class Exactness(Enum):
    """
    Exactness, to specify for specific operator the implementation preference (default and local).
    """

    APPROXIMATE = "approximate"
    EXACT = "exact"


@dataclass
class ApproximateRoundingConfig:
    """
    Controls the behavior of approximate rounding.

    In the following `k` is the ideal rounding output precision.
    Often the precision used after rounding is `k`+1 to avoid overflow.
    `logical_clipping`, `approximate_clipping_start_precision` can be used to stay at precision `k`,
    either logically or physically at the successor TLU.
    See examples in https://github.com/zama-ai/concrete/blob/main/docs/core-features/rounding.md.
    """

    logical_clipping: bool = True
    """
    Enable logical clipping to simulate a precision `k` in the successor TLU of precision `k`+1.
    """

    approximate_clipping_start_precision: int = 5
    """Actively avoid the overflow using a `k`-1 precision TLU.
    This is similar to logical clipping but less accurate and faster.
    Effect on:
    * accuracy: the upper values of the rounding range are slightly decreased,
    * cost: adds an extra `k`-1 bits TLU to guarantee that the precision after rounding is `k`.
            This is usually a win when `k` >= 5 .
    This is enabled by default for `k` >= 5.
    Due to the extra inaccuracy and cost, it is possible to disable it completely using False."""

    reduce_precision_after_approximate_clipping: bool = True
    """Enable the reduction to `k` bits in the TLU.
    Can be disabled for debugging/testing purposes.
    When disabled along with logical_clipping, the result of approximate clipping is accessible.
    """

    symetrize_deltas: bool = True
    """Enable asymetry of correction of deltas w.r.t. the exact rounding computation.
    Can be disabled for debugging/testing purposes.
    """


class ComparisonStrategy(str, Enum):
    """
    ComparisonStrategy, to specify implementation preference for comparisons.
    """

    ONE_TLU_PROMOTED = "one-tlu-promoted"
    # ---------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits -> 9-bits
    # - y :: 8-bits -> 9-bits
    # - x.bit_width == y.bit_width
    #
    # execution:
    # - tlu(x - y) :: 9-bits -> 1-bits

    THREE_TLU_CASTED = "three-tlu-casted"
    # ---------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 9-bits
    # - y = tlu(y) :: 8-bits -> 9-bits
    # - tlu(x - y) :: 9-bits -> 1-bits

    TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED = "two-tlu-bigger-promoted-smaller-casted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits -> 9-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 9-bits
    # - tlu(x - y) :: 9-bits -> 1-bits

    TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED = "two-tlu-bigger-casted-smaller-promoted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits -> 9-bits
    # - y :: 8-bits
    #
    # execution:
    # - y = tlu(y) :: 8-bits -> 9-bits
    # - tlu(x - y) :: 9-bits -> 1-bits

    THREE_TLU_BIGGER_CLIPPED_SMALLER_CASTED = "three-tlu-bigger-clipped-smaller-casted"
    # -------------------------------------------------------------------------------
    # conditions:
    # - x.bit_width != y.bit_width
    # - smaller = x if x.bit_width < y.bit_width else y
    #   bigger = x if x.bit_width > y.bit_width else y
    #   clipped(value) = np.clip(value, smaller.min() - 1, smaller.max() + 1)
    #   any(
    #       (
    #           bit_width <= MAXIMUM_TLU_BIT_WIDTH and
    #           bit_width <= bigger.dtype.bit_width and
    #           bit_width > smaller.dtype.bit_width
    #       )
    #       for bit_width in [
    #           (smaller - clipped(bigger)).bit_width,
    #           (clipped(bigger) - smaller).bit_width,
    #       ]
    #   )
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 5-bits
    # - y = tlu(y) :: 8-bits -> 5-bits
    # - tlu(x - y) :: 5-bits -> 1-bits

    TWO_TLU_BIGGER_CLIPPED_SMALLER_PROMOTED = "two-tlu-bigger-clipped-smaller-promoted"
    # -------------------------------------------------------------------------------
    # conditions:
    # - x.bit_width != y.bit_width
    # - smaller = x if x.bit_width < y.bit_width else y
    #   bigger = x if x.bit_width > y.bit_width else y
    #   clipped(value) = np.clip(value, smaller.min() - 1, smaller.max() + 1)
    #   any(
    #       (
    #           bit_width <= MAXIMUM_TLU_BIT_WIDTH and
    #           bit_width <= bigger.dtype.bit_width and
    #           bit_width > smaller.dtype.bit_width
    #       )
    #       for bit_width in [
    #           (smaller - clipped(bigger)).bit_width,
    #           (clipped(bigger) - smaller).bit_width,
    #       ]
    #   )
    #
    # bit-width assignment:
    # - x :: 3-bits -> 4-bits
    # - y :: 8-bits
    #
    # execution:
    # - y = tlu(y) :: 8-bits -> 4-bits
    # - tlu(x - y) :: 4-bits -> 1-bits

    CHUNKED = "chunked"
    # ---------------
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    #
    # execution:
    # - at least 5 TLUs
    # - at most 13 TLUs
    # - it's complicated...

    @classmethod
    def parse(cls, string: str) -> "ComparisonStrategy":
        """
        Convert a string to a ComparisonStrategy.
        """

        if isinstance(string, cls):
            return string

        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)

        string = string.lower()
        for value in ComparisonStrategy:
            if string == value.value:
                return value  # pragma: no cover

        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in ComparisonStrategy)})"
        )
        raise ValueError(message)

    def can_be_used(self, x: ValueDescription, y: ValueDescription) -> bool:
        """
        Get if the strategy can be used for the comparison.

        Args:
            x (ValueDescription):
                description of the lhs of the comparison

            y (ValueDescription):
                description of the rhs of the comparison

        Returns:
            bool:
                whether the strategy can be used for the comparison
        """

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        if self in {
            ComparisonStrategy.ONE_TLU_PROMOTED,
            ComparisonStrategy.THREE_TLU_CASTED,
            ComparisonStrategy.TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED,
            ComparisonStrategy.TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED,
        }:
            x_minus_y_min = x.dtype.min() - y.dtype.max()
            x_minus_y_max = x.dtype.max() - y.dtype.min()

            x_minus_y_range = [x_minus_y_min, x_minus_y_max]
            x_minus_y_dtype = Integer.that_can_represent(x_minus_y_range)

            if x_minus_y_dtype.bit_width > MAXIMUM_TLU_BIT_WIDTH:  # pragma: no cover
                return False

        if self in {
            ComparisonStrategy.THREE_TLU_BIGGER_CLIPPED_SMALLER_CASTED,
            ComparisonStrategy.TWO_TLU_BIGGER_CLIPPED_SMALLER_PROMOTED,
        }:
            if x.dtype.bit_width == y.dtype.bit_width:
                return False

            smaller = x if x.dtype.bit_width < y.dtype.bit_width else y
            bigger = x if x.dtype.bit_width > y.dtype.bit_width else y

            assert isinstance(smaller.dtype, Integer)
            assert isinstance(bigger.dtype, Integer)

            smaller_bounds = [
                smaller.dtype.min(),
                smaller.dtype.max(),
            ]
            clipped_bigger_bounds = [
                np.clip(smaller_bounds[0] - 1, bigger.dtype.min(), bigger.dtype.max()),
                np.clip(smaller_bounds[1] + 1, bigger.dtype.min(), bigger.dtype.max()),
            ]

            assert clipped_bigger_bounds[0] >= smaller_bounds[0] - 1
            assert clipped_bigger_bounds[1] <= smaller_bounds[1] + 1

            assert clipped_bigger_bounds[0] >= bigger.dtype.min()
            assert clipped_bigger_bounds[1] <= bigger.dtype.max()

            smaller_minus_clipped_bigger_range = [
                smaller_bounds[0] - clipped_bigger_bounds[1],
                smaller_bounds[1] - clipped_bigger_bounds[0],
            ]
            smaller_minus_clipped_bigger_dtype = Integer.that_can_represent(
                smaller_minus_clipped_bigger_range
            )

            clipped_bigger_minus_smaller_range = [
                clipped_bigger_bounds[0] - smaller_bounds[1],
                clipped_bigger_bounds[1] - smaller_bounds[0],
            ]
            clipped_bigger_minus_smaller_dtype = Integer.that_can_represent(
                clipped_bigger_minus_smaller_range
            )

            if all(
                (
                    bit_width > MAXIMUM_TLU_BIT_WIDTH
                    or bit_width > bigger.dtype.bit_width
                    or bit_width <= smaller.dtype.bit_width
                )
                for bit_width in [
                    smaller_minus_clipped_bigger_dtype.bit_width,
                    clipped_bigger_minus_smaller_dtype.bit_width,
                ]
            ):
                return False

        return True

    def promotions(self, x: ValueDescription, y: ValueDescription) -> tuple[int, int]:
        """
        Get bit-width promotions for the strategy.

        Args:
            x (ValueDescription):
                description of the lhs of the comparison

            y (ValueDescription):
                description of the rhs of the comparison

        Returns:
            Tuple[int, int]:
                required minimum bit-width for x and y to use the strategy
        """

        def _promotions(
            smaller_dtype: Integer,
            bigger_dtype: Integer,
            subtraction_dtype: Integer,
        ) -> tuple[int, int]:
            smaller_bit_width = smaller_dtype.bit_width
            bigger_bit_width = bigger_dtype.bit_width
            subtraction_bit_width = subtraction_dtype.bit_width

            if self == ComparisonStrategy.ONE_TLU_PROMOTED:
                assert subtraction_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    subtraction_bit_width,
                    subtraction_bit_width,
                )

            if self == ComparisonStrategy.TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED:
                assert subtraction_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    (
                        smaller_bit_width
                        if smaller_bit_width != bigger_bit_width
                        else subtraction_bit_width
                    ),
                    subtraction_bit_width,
                )

            if self == ComparisonStrategy.TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED:
                assert subtraction_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    subtraction_bit_width,
                    (
                        bigger_bit_width
                        if bigger_bit_width != smaller_bit_width
                        else subtraction_bit_width
                    ),
                )

            if self == ComparisonStrategy.TWO_TLU_BIGGER_CLIPPED_SMALLER_PROMOTED:
                assert smaller_bit_width != bigger_bit_width

                smaller_bounds = [
                    smaller_dtype.min(),
                    smaller_dtype.max(),
                ]
                clipped_bigger_bounds = [
                    np.clip(smaller_bounds[0] - 1, bigger_dtype.min(), bigger_dtype.max()),
                    np.clip(smaller_bounds[1] + 1, bigger_dtype.min(), bigger_dtype.max()),
                ]

                assert clipped_bigger_bounds[0] >= smaller_bounds[0] - 1
                assert clipped_bigger_bounds[1] <= smaller_bounds[1] + 1

                assert clipped_bigger_bounds[0] >= bigger_dtype.min()
                assert clipped_bigger_bounds[1] <= bigger_dtype.max()

                smaller_minus_clipped_bigger_range = [
                    smaller_bounds[0] - clipped_bigger_bounds[1],
                    smaller_bounds[1] - clipped_bigger_bounds[0],
                ]
                smaller_minus_clipped_bigger_dtype = Integer.that_can_represent(
                    smaller_minus_clipped_bigger_range
                )

                clipped_bigger_minus_smaller_range = [
                    clipped_bigger_bounds[0] - smaller_bounds[1],
                    clipped_bigger_bounds[1] - smaller_bounds[0],
                ]
                clipped_bigger_minus_smaller_dtype = Integer.that_can_represent(
                    clipped_bigger_minus_smaller_range
                )

                intermediate_bit_width = min(
                    smaller_minus_clipped_bigger_dtype.bit_width,
                    clipped_bigger_minus_smaller_dtype.bit_width,
                )

                assert intermediate_bit_width > smaller_bit_width
                assert intermediate_bit_width <= bigger_bit_width

                return (
                    intermediate_bit_width,
                    bigger_bit_width,
                )

            return (
                smaller_bit_width,
                bigger_bit_width,
            )

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        x_minus_y_min = x.dtype.min() - y.dtype.max()
        x_minus_y_max = x.dtype.max() - y.dtype.min()

        x_minus_y_range = [x_minus_y_min, x_minus_y_max]
        x_minus_y_dtype = Integer.that_can_represent(x_minus_y_range)

        if x.dtype.bit_width <= y.dtype.bit_width:
            required_x_bit_width, required_y_bit_width = _promotions(
                smaller_dtype=x.dtype,
                bigger_dtype=y.dtype,
                subtraction_dtype=x_minus_y_dtype,
            )
        else:
            required_y_bit_width, required_x_bit_width = _promotions(
                smaller_dtype=y.dtype,
                bigger_dtype=x.dtype,
                subtraction_dtype=x_minus_y_dtype,
            )

        return required_x_bit_width, required_y_bit_width


class BitwiseStrategy(str, Enum):
    """
    BitwiseStrategy, to specify implementation preference for bitwise operations.
    """

    ONE_TLU_PROMOTED = "one-tlu-promoted"
    # ---------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits -> 11-bits
    # - y :: 8-bits -> 11-bits
    # - x.bit_width == y.bit_width
    #
    # execution:
    # - tlu(pack(x, y)) :: 11-bits -> 8-bits

    THREE_TLU_CASTED = "three-tlu-casted"
    # ---------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 11-bits
    # - y = tlu(y) :: 8-bits -> 11-bits
    # - tlu(pack(x, y)) :: 11-bits -> 8-bits

    TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED = "two-tlu-bigger-promoted-smaller-casted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits -> 11-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 11-bits
    # - tlu(pack(x, y)) :: 11-bits -> 8-bits

    TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED = "two-tlu-bigger-casted-smaller-promoted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits -> 11-bits
    # - y :: 8-bits
    #
    # execution:
    # - y = tlu(y) :: 8-bits -> 11-bits
    # - tlu(pack(x, y)) :: 11-bits -> 8-bits

    CHUNKED = "chunked"
    # ---------------
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    #
    # execution:
    # - at least 4 TLUs
    # - at most 9 TLUs
    # - it's complicated...

    @classmethod
    def parse(cls, string: str) -> "BitwiseStrategy":
        """
        Convert a string to a BitwiseStrategy.
        """

        if isinstance(string, cls):
            return string

        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)

        string = string.lower()
        for value in BitwiseStrategy:
            if string == value.value:
                return value  # pragma: no cover

        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in BitwiseStrategy)})"
        )
        raise ValueError(message)

    def can_be_used(self, x: ValueDescription, y: ValueDescription) -> bool:
        """
        Get if the strategy can be used for the bitwise operation.

        Args:
            x (ValueDescription):
                description of the lhs of the bitwise operation

            y (ValueDescription):
                description of the rhs of the bitwise operation

        Returns:
            bool:
                whether the strategy can be used for the bitwise operation
        """

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        if self in {
            BitwiseStrategy.ONE_TLU_PROMOTED,
            BitwiseStrategy.THREE_TLU_CASTED,
            BitwiseStrategy.TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED,
            BitwiseStrategy.TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED,
        }:
            if x.dtype.bit_width + y.dtype.bit_width > MAXIMUM_TLU_BIT_WIDTH:  # pragma: no cover
                return False

        return True

    def promotions(self, x: ValueDescription, y: ValueDescription) -> tuple[int, int]:
        """
        Get bit-width promotions for the strategy.

        Args:
            x (ValueDescription):
                description of the lhs of the bitwise operation

            y (ValueDescription):
                description of the rhs of the bitwise operation

        Returns:
            Tuple[int, int]:
                required minimum bit-width for x and y to use the strategy
        """

        def _promotions(
            smaller_dtype: Integer,
            bigger_dtype: Integer,
        ) -> tuple[int, int]:
            smaller_bit_width = smaller_dtype.bit_width
            bigger_bit_width = bigger_dtype.bit_width
            packing_bit_width = smaller_bit_width + bigger_bit_width

            if self == BitwiseStrategy.ONE_TLU_PROMOTED:
                assert packing_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    packing_bit_width,
                    packing_bit_width,
                )

            if self == BitwiseStrategy.TWO_TLU_BIGGER_PROMOTED_SMALLER_CASTED:
                assert packing_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    (
                        smaller_bit_width
                        if smaller_bit_width != bigger_bit_width
                        else packing_bit_width
                    ),
                    packing_bit_width,
                )

            if self == BitwiseStrategy.TWO_TLU_BIGGER_CASTED_SMALLER_PROMOTED:
                assert packing_bit_width <= MAXIMUM_TLU_BIT_WIDTH
                return (
                    packing_bit_width,
                    (
                        bigger_bit_width
                        if bigger_bit_width != smaller_bit_width
                        else packing_bit_width
                    ),
                )

            return (
                smaller_bit_width,
                bigger_bit_width,
            )

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        if x.dtype.bit_width <= y.dtype.bit_width:
            required_x_bit_width, required_y_bit_width = _promotions(
                smaller_dtype=x.dtype,
                bigger_dtype=y.dtype,
            )
        else:
            required_y_bit_width, required_x_bit_width = _promotions(
                smaller_dtype=y.dtype,
                bigger_dtype=x.dtype,
            )

        return required_x_bit_width, required_y_bit_width


class MultivariateStrategy(str, Enum):
    """
    MultivariateStrategy, to specify implementation preference for multivariate operations.
    """

    PROMOTED = "promoted"
    # ---------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width + ...) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits -> 13-bits
    # - y :: 8-bits -> 13-bits
    # - z :: 2-bits -> 13-bits
    # - x.bit_width == y.bit_width == z.bit_width
    #
    # execution:
    # - tlu(pack(x, y, z)) :: 13-bits -> 8-bits

    CASTED = "casted"
    # ---------------------------------
    # conditions:
    # - (x.bit_width + y.bit_width + ...) <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 3-bits
    # - y :: 8-bits
    # - z :: 2-bits
    #
    # execution:
    # - x = tlu(x) :: 3-bits -> 13-bits
    # - y = tlu(y) :: 8-bits -> 13-bits
    # - z = tlu(z) :: 2-bits -> 13-bits
    # - tlu(pack(x, y, z)) :: 13-bits -> 8-bits

    @classmethod
    def parse(cls, string: str) -> "MultivariateStrategy":
        """
        Convert a string to a MultivariateStrategy.
        """

        if isinstance(string, cls):
            return string

        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)

        string = string.lower()
        for value in MultivariateStrategy:
            if string == value.value:
                return value  # pragma: no cover

        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in MultivariateStrategy)})"
        )
        raise ValueError(message)

    def can_be_used(self, *args: ValueDescription) -> bool:
        """
        Get if the strategy can be used for the multivariate operation.

        Args:
            args (Tuple[ValueDescription]):
                description of the arguments of the multivariate operation

        Returns:
            bool:
                whether the strategy can be used for the multivariate operation
        """

        sum_of_bit_widths = 0
        for arg in args:
            assert isinstance(arg.dtype, Integer)
            sum_of_bit_widths += arg.dtype.bit_width

        return sum_of_bit_widths <= MAXIMUM_TLU_BIT_WIDTH

    def promotions(self, *args: ValueDescription) -> tuple[int, ...]:
        """
        Get bit-width promotions for the strategy.

        Args:
            args (Tuple[ValueDescription]):
                description of the arguments of the multivariate operation

        Returns:
            Tuple[int, int]:
                required minimum bit-width for the arguments to use the strategy
        """

        sum_of_bit_widths = 0
        for arg in args:
            assert isinstance(arg.dtype, Integer)
            sum_of_bit_widths += arg.dtype.bit_width

        result = []
        for arg in args:
            assert isinstance(arg.dtype, Integer)
            required_bit_width = arg.dtype.bit_width

            if self == MultivariateStrategy.PROMOTED:
                required_bit_width = sum_of_bit_widths
            result.append(required_bit_width)

        return tuple(result)


class MinMaxStrategy(str, Enum):
    """
    MinMaxStrategy, to specify implementation preference for minimum and maximum operations.
    """

    ONE_TLU_PROMOTED = "one-tlu-promoted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 8-bits -> 9-bits
    # - y :: 3-bits -> 9-bits
    #
    # execution:
    # - tlu(x - y) + y :: (9-bits -> 9-bits) + 9-bits

    THREE_TLU_CASTED = "three-tlu-casted"
    # -----------------------------------------------------------------------------
    # conditions:
    # - (x - y).bit_width <= MAXIMUM_TLU_BIT_WIDTH
    #
    # bit-width assignment:
    # - x :: 8-bits
    # - y :: 3-bits
    #
    # execution:
    # - x = tlu(x) :: 8-bits -> 9-bits
    # - y = tlu(y) :: 3-bits -> 9-bits
    # - (
    # -   tlu(x - y) + y :: (9-bits -> 3-bits) + 3-bits
    # -   or
    # -   tlu(x - y) + tlu(y) :: (9-bits -> 8-bits) + (3-bits -> 8-bits)
    # - )

    CHUNKED = "chunked"
    # ---------------
    # bit-width assignment:
    # - x :: 8-bits
    # - y :: 3-bits
    #
    # execution:
    # - at least 9 TLUs
    # - at most 21 TLUs
    # - it's complicated...

    @classmethod
    def parse(cls, string: str) -> "MinMaxStrategy":
        """
        Convert a string to a MinMaxStrategy.
        """

        if isinstance(string, cls):
            return string

        if not isinstance(string, str):
            message = f"{string} cannot be parsed to a {cls.__name__}"
            raise TypeError(message)

        string = string.lower()
        for value in MinMaxStrategy:
            if string == value.value:
                return value  # pragma: no cover

        message = (
            f"'{string}' is not a valid '{friendly_type_format(cls)}' ("
            f"{', '.join(v.value for v in MinMaxStrategy)})"
        )
        raise ValueError(message)

    def can_be_used(self, x: ValueDescription, y: ValueDescription) -> bool:
        """
        Get if the strategy can be used for the operation.

        Args:
            x (ValueDescription):
                description of the lhs of the operation

            y (ValueDescription):
                description of the rhs of the operation

        Returns:
            bool:
                whether the strategy can be used for the operation
        """

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        if self in {
            MinMaxStrategy.ONE_TLU_PROMOTED,
            MinMaxStrategy.THREE_TLU_CASTED,
        }:
            x_minus_y_min = x.dtype.min() - y.dtype.max()
            x_minus_y_max = x.dtype.max() - y.dtype.min()

            x_minus_y_range = [x_minus_y_min, x_minus_y_max]
            x_minus_y_dtype = Integer.that_can_represent(x_minus_y_range)

            if x_minus_y_dtype.bit_width > MAXIMUM_TLU_BIT_WIDTH:  # pragma: no cover
                return False

        return True

    def promotions(self, x: ValueDescription, y: ValueDescription) -> tuple[int, int]:
        """
        Get bit-width promotions for the strategy.

        Args:
            x (ValueDescription):
                description of the lhs of the operation

            y (ValueDescription):
                description of the rhs of the operation

        Returns:
            Tuple[int, int]:
                required minimum bit-width for x and y to use the strategy
        """

        assert isinstance(x.dtype, Integer)
        assert isinstance(y.dtype, Integer)

        if self == MinMaxStrategy.ONE_TLU_PROMOTED:
            x_minus_y_min = x.dtype.min() - y.dtype.max()
            x_minus_y_max = x.dtype.max() - y.dtype.min()

            x_minus_y_range = [x_minus_y_min, x_minus_y_max]
            x_minus_y_dtype = Integer.that_can_represent(x_minus_y_range)

            return x_minus_y_dtype.bit_width, x_minus_y_dtype.bit_width

        return x.dtype.bit_width, y.dtype.bit_width


class Configuration:
    """
    Configuration class, to allow the compilation process to be customized.
    """

    verbose: bool
    show_graph: Optional[bool]
    show_bit_width_constraints: Optional[bool]
    show_bit_width_assignments: Optional[bool]
    show_assigned_graph: Optional[bool]
    show_mlir: Optional[bool]
    show_optimizer: Optional[bool]
    show_statistics: Optional[bool]
    dump_artifacts_on_unexpected_failures: bool
    enable_unsafe_features: bool
    use_insecure_key_cache: bool
    loop_parallelize: bool
    dataflow_parallelize: bool
    auto_parallelize: bool
    compress_evaluation_keys: bool
    compress_input_ciphertexts: bool
    p_error: Optional[float]
    global_p_error: Optional[float]
    insecure_key_cache_location: Optional[str]
    auto_adjust_rounders: bool
    auto_adjust_truncators: bool
    single_precision: bool
    parameter_selection_strategy: ParameterSelectionStrategy
    multi_parameter_strategy: MultiParameterStrategy
    show_progress: bool
    progress_title: str
    progress_tag: Union[bool, int]
    fhe_simulation: bool
    fhe_execution: bool
    compiler_debug_mode: bool
    compiler_verbose_mode: bool
    comparison_strategy_preference: list[ComparisonStrategy]
    bitwise_strategy_preference: list[BitwiseStrategy]
    shifts_with_promotion: bool
    multivariate_strategy_preference: list[MultivariateStrategy]
    min_max_strategy_preference: list[MinMaxStrategy]
    use_gpu: bool
    relu_on_bits_threshold: int
    relu_on_bits_chunk_size: int
    if_then_else_chunk_size: int
    additional_pre_processors: list[GraphProcessor]
    additional_post_processors: list[GraphProcessor]
    rounding_exactness: Exactness
    approximate_rounding_config: ApproximateRoundingConfig
    optimize_tlu_based_on_measured_bounds: bool
    enable_tlu_fusing: bool
    print_tlu_fusing: bool
    optimize_tlu_based_on_original_bit_width: Union[bool, int]
    detect_overflow_in_simulation: bool
    dynamic_indexing_check_out_of_bounds: bool
    dynamic_assignment_check_out_of_bounds: bool
    simulate_encrypt_run_decrypt: bool
    composable: bool
    range_restriction: Optional[RangeRestriction]
    keyset_restriction: Optional[KeysetRestriction]
    auto_schedule_run: bool
    security_level: SecurityLevel
    optim_lsbs_with_lut: bool

    def __init__(
        self,
        *,
        verbose: bool = False,
        show_graph: Optional[bool] = None,
        show_bit_width_constraints: Optional[bool] = None,
        show_bit_width_assignments: Optional[bool] = None,
        show_assigned_graph: Optional[bool] = None,
        show_mlir: Optional[bool] = None,
        show_optimizer: Optional[bool] = None,
        show_statistics: Optional[bool] = None,
        dump_artifacts_on_unexpected_failures: bool = True,
        enable_unsafe_features: bool = False,
        use_insecure_key_cache: bool = False,
        insecure_key_cache_location: Optional[Union[Path, str]] = None,
        loop_parallelize: bool = True,
        dataflow_parallelize: bool = False,
        auto_parallelize: bool = False,
        compress_evaluation_keys: bool = False,
        compress_input_ciphertexts: bool = False,
        p_error: Optional[float] = None,
        global_p_error: Optional[float] = None,
        auto_adjust_rounders: bool = False,
        auto_adjust_truncators: bool = False,
        single_precision: bool = False,
        parameter_selection_strategy: Union[
            ParameterSelectionStrategy, str
        ] = ParameterSelectionStrategy.MULTI,
        multi_parameter_strategy: Union[
            MultiParameterStrategy, str
        ] = MultiParameterStrategy.PRECISION,
        show_progress: bool = False,
        progress_title: str = "",
        progress_tag: Union[bool, int] = False,
        fhe_simulation: bool = False,
        fhe_execution: bool = True,
        compiler_debug_mode: bool = False,
        compiler_verbose_mode: bool = False,
        comparison_strategy_preference: Optional[
            Union[ComparisonStrategy, str, list[Union[ComparisonStrategy, str]]]
        ] = None,
        bitwise_strategy_preference: Optional[
            Union[BitwiseStrategy, str, list[Union[BitwiseStrategy, str]]]
        ] = None,
        shifts_with_promotion: bool = True,
        multivariate_strategy_preference: Optional[
            Union[MultivariateStrategy, str, list[Union[MultivariateStrategy, str]]]
        ] = None,
        min_max_strategy_preference: Optional[
            Union[MinMaxStrategy, str, list[Union[MinMaxStrategy, str]]]
        ] = None,
        composable: bool = False,
        use_gpu: bool = False,
        relu_on_bits_threshold: int = 7,
        relu_on_bits_chunk_size: int = 3,
        if_then_else_chunk_size: int = 3,
        additional_pre_processors: Optional[list[GraphProcessor]] = None,
        additional_post_processors: Optional[list[GraphProcessor]] = None,
        rounding_exactness: Exactness = Exactness.EXACT,
        approximate_rounding_config: Optional[ApproximateRoundingConfig] = None,
        optimize_tlu_based_on_measured_bounds: bool = False,
        enable_tlu_fusing: bool = True,
        print_tlu_fusing: bool = False,
        optimize_tlu_based_on_original_bit_width: Union[bool, int] = 8,
        detect_overflow_in_simulation: bool = False,
        dynamic_indexing_check_out_of_bounds: bool = True,
        dynamic_assignment_check_out_of_bounds: bool = True,
        simulate_encrypt_run_decrypt: bool = False,
        range_restriction: Optional[RangeRestriction] = None,
        keyset_restriction: Optional[KeysetRestriction] = None,
        auto_schedule_run: bool = False,
        security_level: SecurityLevel = SecurityLevel.SECURITY_128_BITS,
        optim_lsbs_with_lut: bool = True,
    ):
        self.verbose = verbose
        self.compiler_debug_mode = compiler_debug_mode
        self.compiler_verbose_mode = compiler_verbose_mode
        self.show_graph = show_graph
        self.show_bit_width_constraints = show_bit_width_constraints
        self.show_bit_width_assignments = show_bit_width_assignments
        self.show_assigned_graph = show_assigned_graph
        self.show_mlir = show_mlir
        self.show_optimizer = show_optimizer
        self.show_statistics = show_statistics
        self.dump_artifacts_on_unexpected_failures = dump_artifacts_on_unexpected_failures
        self.enable_unsafe_features = enable_unsafe_features
        self.use_insecure_key_cache = use_insecure_key_cache
        self.insecure_key_cache_location = (
            str(insecure_key_cache_location)
            if isinstance(insecure_key_cache_location, Path)
            else insecure_key_cache_location
        )
        self.loop_parallelize = loop_parallelize
        self.dataflow_parallelize = dataflow_parallelize
        self.auto_parallelize = auto_parallelize
        self.compress_evaluation_keys = compress_evaluation_keys
        self.compress_input_ciphertexts = compress_input_ciphertexts
        self.p_error = p_error
        self.global_p_error = global_p_error
        self.auto_adjust_rounders = auto_adjust_rounders
        self.auto_adjust_truncators = auto_adjust_truncators
        self.single_precision = single_precision
        self.parameter_selection_strategy = ParameterSelectionStrategy.parse(
            parameter_selection_strategy
        )
        self.multi_parameter_strategy = MultiParameterStrategy.parse(multi_parameter_strategy)
        self.show_progress = show_progress
        self.progress_title = progress_title
        self.progress_tag = progress_tag
        self.fhe_simulation = fhe_simulation
        self.fhe_execution = fhe_execution
        self.comparison_strategy_preference = (
            []
            if comparison_strategy_preference is None
            else (
                [ComparisonStrategy.parse(strategy) for strategy in comparison_strategy_preference]
                if isinstance(comparison_strategy_preference, list)
                else [ComparisonStrategy.parse(comparison_strategy_preference)]
            )
        )
        self.bitwise_strategy_preference = (
            []
            if bitwise_strategy_preference is None
            else (
                [BitwiseStrategy.parse(strategy) for strategy in bitwise_strategy_preference]
                if isinstance(bitwise_strategy_preference, list)
                else [BitwiseStrategy.parse(bitwise_strategy_preference)]
            )
        )
        self.shifts_with_promotion = shifts_with_promotion
        self.multivariate_strategy_preference = (
            []
            if multivariate_strategy_preference is None
            else (
                [
                    MultivariateStrategy.parse(strategy)
                    for strategy in multivariate_strategy_preference
                ]
                if isinstance(multivariate_strategy_preference, list)
                else [MultivariateStrategy.parse(multivariate_strategy_preference)]
            )
        )
        self.min_max_strategy_preference = (
            []
            if min_max_strategy_preference is None
            else (
                [MinMaxStrategy.parse(strategy) for strategy in min_max_strategy_preference]
                if isinstance(min_max_strategy_preference, list)
                else [MinMaxStrategy.parse(min_max_strategy_preference)]
            )
        )
        self.composable = composable
        self.use_gpu = use_gpu
        self.relu_on_bits_threshold = relu_on_bits_threshold
        self.relu_on_bits_chunk_size = relu_on_bits_chunk_size
        self.if_then_else_chunk_size = if_then_else_chunk_size
        self.additional_pre_processors = (
            [] if additional_pre_processors is None else additional_pre_processors
        )
        self.additional_post_processors = (
            [] if additional_post_processors is None else additional_post_processors
        )
        self.rounding_exactness = rounding_exactness
        self.approximate_rounding_config = (
            approximate_rounding_config or ApproximateRoundingConfig()
        )
        self.optimize_tlu_based_on_measured_bounds = optimize_tlu_based_on_measured_bounds

        self.enable_tlu_fusing = enable_tlu_fusing
        self.print_tlu_fusing = print_tlu_fusing

        self.optimize_tlu_based_on_original_bit_width = optimize_tlu_based_on_original_bit_width

        self.detect_overflow_in_simulation = detect_overflow_in_simulation

        self.dynamic_indexing_check_out_of_bounds = dynamic_indexing_check_out_of_bounds
        self.dynamic_assignment_check_out_of_bounds = dynamic_assignment_check_out_of_bounds

        self.simulate_encrypt_run_decrypt = simulate_encrypt_run_decrypt
        self.range_restriction = range_restriction
        self.keyset_restriction = keyset_restriction

        self.auto_schedule_run = auto_schedule_run

        self.security_level = security_level

        self.optim_lsbs_with_lut = optim_lsbs_with_lut

        self._validate()

    class Keep:
        """Keep previous arg value during fork."""

    KEEP = Keep()

    def fork(
        self,
        /,
        # pylint: disable=unused-argument
        verbose: Union[Keep, bool] = KEEP,
        show_graph: Union[Keep, Optional[bool]] = KEEP,
        show_bit_width_constraints: Union[Keep, Optional[bool]] = KEEP,
        show_bit_width_assignments: Union[Keep, Optional[bool]] = KEEP,
        show_assigned_graph: Union[Keep, Optional[bool]] = KEEP,
        show_mlir: Union[Keep, Optional[bool]] = KEEP,
        show_optimizer: Union[Keep, Optional[bool]] = KEEP,
        show_statistics: Union[Keep, Optional[bool]] = KEEP,
        dump_artifacts_on_unexpected_failures: Union[Keep, bool] = KEEP,
        enable_unsafe_features: Union[Keep, bool] = KEEP,
        use_insecure_key_cache: Union[Keep, bool] = KEEP,
        insecure_key_cache_location: Union[Keep, Optional[Union[Path, str]]] = KEEP,
        loop_parallelize: Union[Keep, bool] = KEEP,
        dataflow_parallelize: Union[Keep, bool] = KEEP,
        auto_parallelize: Union[Keep, bool] = KEEP,
        compress_evaluation_keys: Union[Keep, bool] = KEEP,
        compress_input_ciphertexts: Union[Keep, bool] = KEEP,
        p_error: Union[Keep, Optional[float]] = KEEP,
        global_p_error: Union[Keep, Optional[float]] = KEEP,
        auto_adjust_rounders: Union[Keep, bool] = KEEP,
        auto_adjust_truncators: Union[Keep, bool] = KEEP,
        single_precision: Union[Keep, bool] = KEEP,
        parameter_selection_strategy: Union[Keep, Union[ParameterSelectionStrategy, str]] = KEEP,
        multi_parameter_strategy: Union[Keep, Union[MultiParameterStrategy, str]] = KEEP,
        show_progress: Union[Keep, bool] = KEEP,
        progress_title: Union[Keep, str] = KEEP,
        progress_tag: Union[Keep, Union[bool, int]] = KEEP,
        fhe_simulation: Union[Keep, bool] = KEEP,
        fhe_execution: Union[Keep, bool] = KEEP,
        compiler_debug_mode: Union[Keep, bool] = KEEP,
        compiler_verbose_mode: Union[Keep, bool] = KEEP,
        comparison_strategy_preference: Union[
            Keep,
            Optional[Union[ComparisonStrategy, str, list[Union[ComparisonStrategy, str]]]],
        ] = KEEP,
        bitwise_strategy_preference: Union[
            Keep,
            Optional[Union[BitwiseStrategy, str, list[Union[BitwiseStrategy, str]]]],
        ] = KEEP,
        shifts_with_promotion: Union[Keep, bool] = KEEP,
        multivariate_strategy_preference: Union[
            Keep,
            Optional[Union[MultivariateStrategy, str, list[Union[MultivariateStrategy, str]]]],
        ] = KEEP,
        min_max_strategy_preference: Union[
            Keep, Optional[Union[MinMaxStrategy, str, list[Union[MinMaxStrategy, str]]]]
        ] = KEEP,
        composable: Union[Keep, bool] = KEEP,
        use_gpu: Union[Keep, bool] = KEEP,
        relu_on_bits_threshold: Union[Keep, int] = KEEP,
        relu_on_bits_chunk_size: Union[Keep, int] = KEEP,
        if_then_else_chunk_size: Union[Keep, int] = KEEP,
        additional_pre_processors: Union[Keep, Optional[list[GraphProcessor]]] = KEEP,
        additional_post_processors: Union[Keep, Optional[list[GraphProcessor]]] = KEEP,
        rounding_exactness: Union[Keep, Exactness] = KEEP,
        approximate_rounding_config: Union[Keep, Optional[ApproximateRoundingConfig]] = KEEP,
        optimize_tlu_based_on_measured_bounds: Union[Keep, bool] = KEEP,
        enable_tlu_fusing: Union[Keep, bool] = KEEP,
        print_tlu_fusing: Union[Keep, bool] = KEEP,
        optimize_tlu_based_on_original_bit_width: Union[Keep, bool, int] = KEEP,
        detect_overflow_in_simulation: Union[Keep, bool] = KEEP,
        dynamic_indexing_check_out_of_bounds: Union[Keep, bool] = KEEP,
        dynamic_assignment_check_out_of_bounds: Union[Keep, bool] = KEEP,
        simulate_encrypt_run_decrypt: Union[Keep, bool] = KEEP,
        range_restriction: Union[Keep, Optional[RangeRestriction]] = KEEP,
        keyset_restriction: Union[Keep, Optional[KeysetRestriction]] = KEEP,
        auto_schedule_run: Union[Keep, bool] = KEEP,
        security_level: Union[Keep, SecurityLevel] = KEEP,
        optim_lsbs_with_lut: Union[Keep, bool] = KEEP,
    ) -> "Configuration":
        """
        Get a new configuration from another one specified changes.

        See Configuration.
        """

        args = locals()
        return Configuration(
            **{
                name: (
                    getattr(self, name)
                    if isinstance(args[name], Configuration.Keep)
                    else args[name]
                )
                for name in get_type_hints(Configuration.__init__)
            }
        )

    def _validate(self):
        """
        Validate configuration.
        """
        for name, hint in get_type_hints(Configuration.__init__).items():
            already_checked_by_parse_methods = [
                "comparison_strategy_preference",
                "bitwise_strategy_preference",
                "multivariate_strategy_preference",
                "min_max_strategy_preference",
            ]
            if name in already_checked_by_parse_methods:
                continue

            if name in ["additional_pre_processors", "additional_post_processors"]:
                attr = getattr(self, name)
                valid = isinstance(attr, list)
                if valid:
                    for processor in attr:
                        valid = valid and isinstance(processor, GraphProcessor)

                if not valid:
                    hint_type = friendly_type_format(hint)
                    value_type = friendly_type_format(type(attr))
                    message = (
                        f"Unexpected type for keyword argument '{name}' "
                        f"(expected '{hint_type}', got '{value_type}')"
                    )
                    raise TypeError(message)

                continue  # pragma: no cover

            original_hint = hint
            value = getattr(self, name)
            if str(hint).startswith("typing.Union") or str(hint).startswith("typing.Optional"):
                if isinstance(value, tuple(hint.__args__)):
                    continue
            elif isinstance(value, hint):
                continue
            hint = friendly_type_format(original_hint)
            value_type = friendly_type_format(type(value))
            message = (
                f"Unexpected type for keyword argument '{name}' "
                f"(expected '{hint}', got '{value_type}')"
            )
            raise TypeError(message)

        if not self.enable_unsafe_features:  # noqa: SIM102
            if self.use_insecure_key_cache:
                message = "Insecure key cache cannot be used without enabling unsafe features"
                raise RuntimeError(message)
            if self.simulate_encrypt_run_decrypt:
                message = (
                    "Simulating encrypt/run/decrypt cannot be used without enabling unsafe features"
                )
                raise RuntimeError(message)

        if self.use_insecure_key_cache and self.insecure_key_cache_location is None:
            message = "Insecure key cache cannot be enabled without specifying its location"
            raise RuntimeError(message)

        if platform.system() == "Darwin" and self.dataflow_parallelize:  # pragma: no cover
            message = "Dataflow parallelism is not available in macOS"
            raise RuntimeError(message)

        if (
            self.composable and self.parameter_selection_strategy == ParameterSelectionStrategy.MONO
        ):  # pragma: no cover
            message = "Composition can not be used with MONO parameter selection strategy"
            raise RuntimeError(message)


def __check_fork_consistency():
    hints_init = get_type_hints(Configuration.__init__)
    hints_fork = get_type_hints(Configuration.fork)
    diff = set.symmetric_difference(set(hints_init), set(hints_fork) - {"return"})
    if diff:  # pragma: no cover
        message = f"Configuration.fork is inconsistent with Configuration for: {diff}"
        raise TypeError(message)
    for name, init_hint in hints_init.items():
        fork_hint = hints_fork[name]
        if Union[Configuration.Keep, init_hint] != fork_hint:  # pragma: no cover
            fork_hint = friendly_type_format(fork_hint)
            init_hint = friendly_type_format(init_hint)
            message = (
                f"Configuration.fork parameter {name}: {fork_hint} is inconsistent"
                f"with Configuration type: {init_hint}"
            )
            raise TypeError(message)


__check_fork_consistency()
