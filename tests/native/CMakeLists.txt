cmake_minimum_required(VERSION 3.20)

project(OpenUSDConnectNativeTests LANGUAGES CXX)

add_subdirectory(../../native/client_core client_core)

add_executable(OpenUSDConnectProtocolTests test_protocol_codec.cpp)
target_link_libraries(OpenUSDConnectProtocolTests PRIVATE OpenUSDConnect::ClientProtocol)
target_include_directories(OpenUSDConnectProtocolTests PRIVATE
    ../../integrations/unreal/OpenUSDConnect/Source/OpenUSDConnectPXR/ThirdParty/flatbuffers/include
)
