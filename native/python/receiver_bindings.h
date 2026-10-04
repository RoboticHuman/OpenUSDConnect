#pragma once

#include <nanobind/nanobind.h>

// Binds the receiver endpoint, the reference driver, and its socket factories.
void BindReceiver(nanobind::module_& module);
