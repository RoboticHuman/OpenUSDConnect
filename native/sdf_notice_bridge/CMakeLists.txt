cmake_minimum_required(VERSION 3.20)

project(openusdconnect_sdf_notice_bridge LANGUAGES CXX)

find_package(pxr CONFIG REQUIRED)

if(TARGET usd_m)
    get_target_property(OUC_PXR_INCLUDE_HINTS usd_m INTERFACE_INCLUDE_DIRECTORIES)
elseif(TARGET sdf)
    get_target_property(OUC_PXR_INCLUDE_HINTS sdf INTERFACE_INCLUDE_DIRECTORIES)
endif()
find_path(OUC_PXR_INCLUDE_DIR pxr/pxr.h
    HINTS ${OUC_PXR_INCLUDE_HINTS} "${pxr_DIR}/include"
    REQUIRED
)
file(STRINGS "${OUC_PXR_INCLUDE_DIR}/pxr/pxr.h" OUC_PXR_VERSION_LINE
    REGEX "^#define PXR_VERSION [0-9]+"
)
string(REGEX MATCH "[0-9]+" OUC_PXR_VERSION "${OUC_PXR_VERSION_LINE}")
file(TO_CMAKE_PATH "${pxr_DIR}" OUC_PXR_DIR)
set(OUC_BRIDGE_ABI_VERSION 1)
set(OUC_SYSTEM_NAME "${CMAKE_SYSTEM_NAME}")
set(OUC_SYSTEM_PROCESSOR "${CMAKE_SYSTEM_PROCESSOR}")
configure_file(
    sdf_notice_bridge.json.in
    sdf_notice_bridge.json
    @ONLY
)

add_library(openusdconnect_sdf_notice_bridge SHARED
    sdf_notice_bridge.cpp
)

target_compile_features(openusdconnect_sdf_notice_bridge PRIVATE cxx_std_17)
target_compile_definitions(openusdconnect_sdf_notice_bridge
    PRIVATE OUC_SDF_NOTICE_BRIDGE_BUILD
)
target_include_directories(openusdconnect_sdf_notice_bridge
    PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}
)

if(TARGET usd_m)
    target_link_libraries(openusdconnect_sdf_notice_bridge PRIVATE usd_m)
elseif(TARGET sdf)
    target_link_libraries(openusdconnect_sdf_notice_bridge PRIVATE sdf tf vt)
else()
    message(FATAL_ERROR "The OpenUSD CMake package exports neither usd_m nor sdf")
endif()

set_target_properties(openusdconnect_sdf_notice_bridge PROPERTIES
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES
    INSTALL_RPATH_USE_LINK_PATH YES
)

add_custom_command(TARGET openusdconnect_sdf_notice_bridge POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        "${CMAKE_CURRENT_BINARY_DIR}/sdf_notice_bridge.json"
        "$<TARGET_FILE:openusdconnect_sdf_notice_bridge>.json"
)
