function(openusdconnect_configure_client_library target)
    set_target_properties(${target} PROPERTIES
        POSITION_INDEPENDENT_CODE ON
        CXX_VISIBILITY_PRESET hidden
        VISIBILITY_INLINES_HIDDEN YES
    )
    if(MSVC)
        target_compile_options(${target} PRIVATE /W4 /permissive-)
    else()
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
    endif()
endfunction()

add_library(OpenUSDConnectClientCore STATIC
    src/frame_codec.cpp
)
add_library(OpenUSDConnect::ClientCore ALIAS OpenUSDConnectClientCore)
target_compile_features(OpenUSDConnectClientCore PUBLIC cxx_std_17)
target_include_directories(OpenUSDConnectClientCore
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include
)
openusdconnect_configure_client_library(OpenUSDConnectClientCore)

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

# Sans-IO endpoints: socket bytes and host time in, actions and notifications out.
add_library(OpenUSDConnectClientEngine STATIC
    src/engine/producer_endpoint.cpp
    src/engine/receiver_endpoint.cpp
)
add_library(OpenUSDConnect::ClientEngine ALIAS OpenUSDConnectClientEngine)
target_link_libraries(OpenUSDConnectClientEngine PUBLIC OpenUSDConnectClientProtocol)
openusdconnect_configure_client_library(OpenUSDConnectClientEngine)

# Optional reference driver: one thread and blocking sockets drive the engine.
# Hosts with their own I/O and scheduler drive the engine directly instead.
add_library(OpenUSDConnectClientDriver STATIC
    src/driver/threaded_producer_driver.cpp
    src/driver/threaded_receiver_driver.cpp
    src/platform/bsd_socket.cpp
)
add_library(OpenUSDConnect::ClientDriver ALIAS OpenUSDConnectClientDriver)
find_package(Threads REQUIRED)
target_link_libraries(OpenUSDConnectClientDriver
    PUBLIC OpenUSDConnectClientEngine Threads::Threads
    PRIVATE $<$<PLATFORM_ID:Windows>:ws2_32>
)
openusdconnect_configure_client_library(OpenUSDConnectClientDriver)

# Scripted sockets that let the C++ tests drive the reference driver without a
# network. Built only for a target that links it; no shipped module does.
add_library(OpenUSDConnectClientDriverTesting STATIC EXCLUDE_FROM_ALL
    src/driver/testing/scripted_socket.cpp
)
add_library(OpenUSDConnect::ClientDriverTesting ALIAS OpenUSDConnectClientDriverTesting)
target_link_libraries(OpenUSDConnectClientDriverTesting PUBLIC OpenUSDConnectClientDriver)
openusdconnect_configure_client_library(OpenUSDConnectClientDriverTesting)
