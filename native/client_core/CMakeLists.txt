add_library(OpenUSDConnectClientCore STATIC
    src/frame_codec.cpp
)
add_library(OpenUSDConnect::ClientCore ALIAS OpenUSDConnectClientCore)

target_compile_features(OpenUSDConnectClientCore PUBLIC cxx_std_17)
target_include_directories(OpenUSDConnectClientCore
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
)
set_target_properties(OpenUSDConnectClientCore PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
)

if(MSVC)
    target_compile_options(OpenUSDConnectClientCore PRIVATE /W4 /permissive-)
else()
    target_compile_options(OpenUSDConnectClientCore PRIVATE -Wall -Wextra -Wpedantic)
endif()

# Header-only FlatBuffers protocol views/builders. Consumers retain control of
# their FlatBuffers allocator and supply the FlatBuffers include target/path.
add_library(OpenUSDConnectClientProtocol INTERFACE)
add_library(OpenUSDConnect::ClientProtocol ALIAS OpenUSDConnectClientProtocol)
target_compile_features(OpenUSDConnectClientProtocol INTERFACE cxx_std_17)
target_include_directories(OpenUSDConnectClientProtocol
    INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(OpenUSDConnectClientProtocol INTERFACE OpenUSDConnectClientCore)

if(TARGET flatbuffers::flatbuffers)
    target_link_libraries(OpenUSDConnectClientProtocol INTERFACE flatbuffers::flatbuffers)
endif()
