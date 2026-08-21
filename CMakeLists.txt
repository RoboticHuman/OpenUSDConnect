cmake_minimum_required(VERSION 3.27)

project(OpenUSDConnectClient LANGUAGES CXX)

file(STRINGS "${CMAKE_CURRENT_SOURCE_DIR}/.python-version"
    OPENUSDCONNECT_PYTHON_VERSION
    LIMIT_COUNT 1
)
if(NOT OPENUSDCONNECT_PYTHON_VERSION MATCHES "^[0-9]+\\.[0-9]+$")
    message(FATAL_ERROR "Invalid or missing .python-version")
endif()

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR
        "OpenUSDConnect requires a 64-bit toolchain to match the supported OpenUSD build."
    )
endif()

find_package(Python ${OPENUSDCONNECT_PYTHON_VERSION} REQUIRED
    COMPONENTS Interpreter Development.Module
    OPTIONAL_COMPONENTS Development.SABIModule
)
find_package(nanobind CONFIG REQUIRED)

add_subdirectory(native/client_core)

nanobind_add_module(_native_client STABLE_ABI
    native/python/client_module.cpp
)
target_link_libraries(_native_client PRIVATE OpenUSDConnectClientCore)
target_compile_features(_native_client PRIVATE cxx_std_17)

if(MSVC)
    target_compile_options(_native_client PRIVATE /W4 /permissive-)
else()
    target_compile_options(_native_client PRIVATE -Wall -Wextra -Wpedantic)
endif()

install(TARGETS _native_client LIBRARY DESTINATION openusdconnect)
