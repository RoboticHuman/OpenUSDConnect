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
add_test(NAME OpenUSDConnectProtocolTests COMMAND OpenUSDConnectProtocolTests)

add_executable(OpenUSDConnectReplayIdentityTests test_replay_identity.cpp)
target_link_libraries(OpenUSDConnectReplayIdentityTests PRIVATE OpenUSDConnect::ClientCore)
add_test(NAME OpenUSDConnectReplayIdentityTests COMMAND OpenUSDConnectReplayIdentityTests)

add_executable(OpenUSDConnectClientEngineTests test_client_engine.cpp)
target_link_libraries(OpenUSDConnectClientEngineTests PRIVATE OpenUSDConnect::ClientProtocol)
add_test(NAME OpenUSDConnectClientEngineTests COMMAND OpenUSDConnectClientEngineTests)

add_executable(OpenUSDConnectReceiverEndpointTests test_receiver_endpoint.cpp)
target_link_libraries(OpenUSDConnectReceiverEndpointTests PRIVATE OpenUSDConnect::ClientEngine)
if(MSVC)
    target_compile_options(OpenUSDConnectReceiverEndpointTests PRIVATE /W4 /permissive-)
else()
    target_compile_options(OpenUSDConnectReceiverEndpointTests PRIVATE -Wall -Wextra -Wpedantic)
endif()
add_test(NAME OpenUSDConnectReceiverEndpointTests COMMAND OpenUSDConnectReceiverEndpointTests)
