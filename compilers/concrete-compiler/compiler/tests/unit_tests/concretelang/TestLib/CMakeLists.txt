add_custom_target(ConcreteCompilerLibTests)

add_dependencies(ConcretelangUnitTests ConcreteCompilerLibTests)

function(add_concretecompiler_lib_test test_name)
  add_unittest(ConcreteCompilerLibTests ${test_name} ${ARGN})
  target_link_libraries(${test_name} PRIVATE ConcretelangSupport ConcretelangClientLib ConcretelangServerLib
                                             ConcretelangRuntime)
  set_source_files_properties(${ARGN} PROPERTIES COMPILE_FLAGS "-fno-rtti")
endfunction()

if(NOT ${CMAKE_SYSTEM_NAME} MATCHES "Darwin")
  link_libraries(
    # useful for old gcc versions
    -Wl,--allow-multiple-definition # static concrete-optimizer and concrete shares some code
  )
endif()

if(CONCRETELANG_DATAFLOW_EXECUTION_ENABLED)
  add_compile_options(-DCONCRETELANG_DATAFLOW_TESTING_ENABLED)
endif()

add_concretecompiler_lib_test(unit_tests_concretelang_testlib testlib_unit_test.cpp)
