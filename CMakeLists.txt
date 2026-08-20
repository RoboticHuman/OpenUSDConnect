cmake_minimum_required(VERSION 3.20)

project(OpenUSDConnectClient LANGUAGES CXX)

find_package(Python 3.13 REQUIRED
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
