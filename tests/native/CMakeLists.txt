cmake_minimum_required(VERSION 3.27)

project(OpenUSDConnectNativeTests LANGUAGES CXX)

enable_testing()

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR
        "OpenUSDConnect requires a 64-bit toolchain to match the supported OpenUSD build."
    )
endif()

add_subdirectory(../../native/client_core client_core)

add_executable(OpenUSDConnectProtocolTests test_protocol_codec.cpp)
target_link_libraries(OpenUSDConnectProtocolTests PRIVATE OpenUSDConnect::ClientProtocol)
target_include_directories(OpenUSDConnectProtocolTests PRIVATE
    ../../integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/ThirdParty/flatbuffers/include
)
add_test(NAME OpenUSDConnectProtocolTests COMMAND OpenUSDConnectProtocolTests)

add_executable(OpenUSDConnectReplayIdentityTests test_replay_identity.cpp)
target_link_libraries(OpenUSDConnectReplayIdentityTests PRIVATE OpenUSDConnect::ClientCore)
add_test(NAME OpenUSDConnectReplayIdentityTests COMMAND OpenUSDConnectReplayIdentityTests)
