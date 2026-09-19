cmake_minimum_required(VERSION 3.27)

project(OpenUSDConnectNativeTests LANGUAGES CXX)

enable_testing()

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR
        "OpenUSDConnect requires a 64-bit toolchain to match the supported OpenUSD build."
    )
endif()

add_subdirectory(../../native/client_core client_core)

set(OPENUSDCONNECT_FLATBUFFERS_INCLUDE_DIR
    "${CMAKE_CURRENT_SOURCE_DIR}/../../integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/ThirdParty/flatbuffers/include"
    CACHE PATH "Directory containing the pinned FlatBuffers C++ headers"
)
if(NOT EXISTS "${OPENUSDCONNECT_FLATBUFFERS_INCLUDE_DIR}/flatbuffers/flatbuffers.h")
    message(FATAL_ERROR
        "FlatBuffers headers are missing. Run "
        "integrations/unreal/OpenUSDConnect/setup_flatbuffers.py before configuring native tests, "
        "or set OPENUSDCONNECT_FLATBUFFERS_INCLUDE_DIR."
    )
endif()

add_executable(OpenUSDConnectProtocolTests test_protocol_codec.cpp)
target_link_libraries(OpenUSDConnectProtocolTests PRIVATE OpenUSDConnect::ClientProtocol)
target_include_directories(OpenUSDConnectProtocolTests PRIVATE
    "${OPENUSDCONNECT_FLATBUFFERS_INCLUDE_DIR}"
)
add_test(NAME OpenUSDConnectProtocolTests COMMAND OpenUSDConnectProtocolTests)

add_executable(OpenUSDConnectReplayIdentityTests test_replay_identity.cpp)
target_link_libraries(OpenUSDConnectReplayIdentityTests PRIVATE OpenUSDConnect::ClientCore)
add_test(NAME OpenUSDConnectReplayIdentityTests COMMAND OpenUSDConnectReplayIdentityTests)
