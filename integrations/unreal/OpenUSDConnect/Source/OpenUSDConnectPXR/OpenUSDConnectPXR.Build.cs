// Copyright OpenUSDConnect Contributors. All Rights Reserved.

using UnrealBuildTool;

public class OpenUSDConnectPXR : ModuleRules
{
	public OpenUSDConnectPXR(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		// pxr headers use typeid. Keep RTTI confined to this pure C++ module;
		// Unreal's UObject modules and base classes are built without it.
		bUseRTTI = true;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"OpenUSDConnectClientCore",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"CoreUObject",
			"Engine",
			"USDStage",
			"USDClasses",
			"USDUtilities",
			"UnrealUSDWrapper",
			"RHI",
		});

		UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);
	}
}
