cmake_minimum_required(VERSION 3.27)

project(OpenUSDConnectBlenderNative LANGUAGES CXX)

if(NOT OPENUSDCONNECT_SOURCE_DIR)
    message(FATAL_ERROR "OPENUSDCONNECT_SOURCE_DIR is required")
endif()

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "OpenUSDConnect host packages require a 64-bit toolchain")
endif()

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(nanobind CONFIG REQUIRED)

add_subdirectory(
    "${OPENUSDCONNECT_SOURCE_DIR}/native/client_core"
    "${CMAKE_CURRENT_BINARY_DIR}/client_core"
)

# Host packages target an exact CPython ABI. A stable-ABI extension cannot
# support applications that still embed Python 3.11.
nanobind_add_module(_native_client
    "${OPENUSDCONNECT_SOURCE_DIR}/native/python/client_module.cpp"
)
target_link_libraries(_native_client PRIVATE OpenUSDConnectClientCore)
target_compile_features(_native_client PRIVATE cxx_std_17)

if(MSVC)
    target_compile_options(_native_client PRIVATE /W4 /permissive-)
else()
    target_compile_options(_native_client PRIVATE -Wall -Wextra -Wpedantic)
endif()
