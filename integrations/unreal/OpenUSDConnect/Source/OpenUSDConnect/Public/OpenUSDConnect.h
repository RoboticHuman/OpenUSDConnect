// Copyright OpenUSDConnect Contributors. All Rights Reserved.
#pragma once

#include "Modules/ModuleManager.h"

class FOpenUSDConnectModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;
};
