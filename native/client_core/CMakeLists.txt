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
# their FlatBuffers allocator.
add_library(OpenUSDConnectClientProtocol INTERFACE)
add_library(OpenUSDConnect::ClientProtocol ALIAS OpenUSDConnectClientProtocol)
target_compile_features(OpenUSDConnectClientProtocol INTERFACE cxx_std_17)
target_include_directories(OpenUSDConnectClientProtocol
    INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_link_libraries(OpenUSDConnectClientProtocol INTERFACE OpenUSDConnectClientCore)

if(TARGET flatbuffers::flatbuffers)
    target_link_libraries(OpenUSDConnectClientProtocol INTERFACE flatbuffers::flatbuffers)
else()
    include(FetchContent)
    # include/ has no CMakeLists.txt, so the FlatBuffers project is never configured.
    FetchContent_Declare(flatbuffers
        URL https://github.com/google/flatbuffers/archive/refs/tags/v25.12.19.tar.gz
        URL_HASH SHA256=f81c3162b1046fe8b84b9a0dbdd383e24fdbcf88583b9cb6028f90d04d90696a
        SOURCE_SUBDIR include
    )
    FetchContent_MakeAvailable(flatbuffers)
    target_include_directories(OpenUSDConnectClientProtocol SYSTEM
        INTERFACE "${flatbuffers_SOURCE_DIR}/include"
    )
endif()
