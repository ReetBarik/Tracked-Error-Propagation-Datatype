cmake_minimum_required(VERSION 3.16)

# Single source of truth for the version is include/tracked/version.hpp (a
# committed header, so subtree/vendored consumers get it without CMake).
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/include/tracked/version.hpp" _tracked_version_hpp)
string(REGEX MATCH "TRACKED_VERSION_MAJOR ([0-9]+)" _ "${_tracked_version_hpp}")
set(_tracked_ver_major "${CMAKE_MATCH_1}")
string(REGEX MATCH "TRACKED_VERSION_MINOR ([0-9]+)" _ "${_tracked_version_hpp}")
set(_tracked_ver_minor "${CMAKE_MATCH_1}")
string(REGEX MATCH "TRACKED_VERSION_PATCH ([0-9]+)" _ "${_tracked_version_hpp}")
set(_tracked_ver_patch "${CMAKE_MATCH_1}")

project(tracked
        VERSION "${_tracked_ver_major}.${_tracked_ver_minor}.${_tracked_ver_patch}"
        LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Debug CACHE STRING "Choose the type of build." FORCE)
endif()

# ---- Tracked<T> header-only library -----------------------------------------

add_library(tracked INTERFACE)
add_library(tracked::tracked ALIAS tracked)
target_include_directories(tracked INTERFACE
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
target_compile_features(tracked INTERFACE cxx_std_17)

# ---- Install / export (find_package(tracked CONFIG)) ------------------------

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

install(TARGETS tracked EXPORT tracked-targets)
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/include/"
        DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}")
install(EXPORT tracked-targets
        NAMESPACE tracked::
        FILE tracked-targets.cmake
        DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/tracked")

configure_package_config_file(
  "${CMAKE_CURRENT_SOURCE_DIR}/cmake/tracked-config.cmake.in"
  "${CMAKE_CURRENT_BINARY_DIR}/tracked-config.cmake"
  INSTALL_DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/tracked")
write_basic_package_version_file(
  "${CMAKE_CURRENT_BINARY_DIR}/tracked-config-version.cmake"
  COMPATIBILITY SameMajorVersion
  ARCH_INDEPENDENT)
install(FILES
  "${CMAKE_CURRENT_BINARY_DIR}/tracked-config.cmake"
  "${CMAKE_CURRENT_BINARY_DIR}/tracked-config-version.cmake"
  DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/tracked")

# ---- Tests ------------------------------------------------------------------
# Guarded so a consumer's add_subdirectory() does not drag in the Catch2
# FetchContent (which needs network at configure time).

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  set(_tracked_is_top_level ON)
else()
  set(_tracked_is_top_level OFF)
endif()
option(TRACKED_BUILD_TESTS "Build the Tracked Catch2 test suite" ${_tracked_is_top_level})

if(TRACKED_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(
    Catch2
    GIT_REPOSITORY https://github.com/catchorg/Catch2.git
    GIT_TAG        v3.4.0
  )
  FetchContent_MakeAvailable(Catch2)

  enable_testing()
  list(APPEND CMAKE_MODULE_PATH "${catch2_SOURCE_DIR}/extras")
  include(Catch)

  foreach(test_name
      test_basic_ops
      test_cancellation
      test_naive_variance
      test_log_sum_exp
      test_kahan_vs_naive
      test_trig
      test_complex_basic
      test_complex_sqrt
      test_complex_log
      test_complex_div
      test_provenance_v03
      test_literals
      test_operand_ids
      test_schema_v1
      test_streaming
      test_new_ops)
    add_executable(${test_name} tests/tracked/${test_name}.cpp)
    target_link_libraries(${test_name} PRIVATE tracked::tracked Catch2::Catch2WithMain)
    catch_discover_tests(${test_name})
  endforeach()

  # Parity-fixture driver (not a ctest test; run manually to regenerate the
  # journals under tests/parity/fixtures/ — see gen_fixtures.cpp for the
  # schema-freeze warning before overwriting committed fixtures).
  add_executable(gen_parity_fixtures tests/parity/gen_fixtures.cpp)
  target_link_libraries(gen_parity_fixtures PRIVATE tracked::tracked)
  # Repo-relative __FILE__ so the committed fixtures are machine-independent.
  target_compile_options(gen_parity_fixtures PRIVATE
    "-fmacro-prefix-map=${CMAKE_CURRENT_SOURCE_DIR}/=")
endif()
