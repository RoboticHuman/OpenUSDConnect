// Shared utilities for the native Sdf bridge DLL.

#include "sdf_delegate_bridge.h"
#include <pxr/pxr.h>
#include <string>

namespace {

thread_local std::string lastError;

void SetError(const char *msg) { lastError = msg; }

} // namespace

extern "C" {

const char *
ouc_sdf_delegate_last_error(void)
{
    return lastError.c_str();
}

uint32_t
ouc_sdf_delegate_pxr_version(void)
{
    return PXR_VERSION;
}

uint32_t
ouc_sdf_delegate_abi_version(void)
{
    return OUC_SDF_DELEGATE_ABI_VERSION;
}

} // extern "C"
