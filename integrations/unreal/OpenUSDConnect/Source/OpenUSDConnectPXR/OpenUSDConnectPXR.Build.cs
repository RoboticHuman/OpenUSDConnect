// Copyright OpenUSDConnect Contributors. All Rights Reserved.

using UnrealBuildTool;
using System.IO;

public class OpenUSDConnectPXR : ModuleRules
{
	public OpenUSDConnectPXR(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicIncludePaths.Add(Path.GetFullPath(Path.Combine(
			ModuleDirectory,
			"../ThirdParty/OpenUSDConnectClientCore/include")));

		// pxr headers use typeid. Keep RTTI confined to this pure C++ module;
		// Unreal's UObject modules and base classes are built without it.
		bUseRTTI = true;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
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

		string LocalInclude = Path.Combine(ModuleDirectory, "ThirdParty", "flatbuffers", "include");
		if (!File.Exists(Path.Combine(LocalInclude, "flatbuffers", "flatbuffer_builder.h")))
		{
			throw new BuildException(
				"OpenUSDConnect: FlatBuffers headers not found. Run  " +
				"python <plugin>/setup_flatbuffers.py  once, then rebuild.");
		}
		PublicSystemIncludePaths.Add(LocalInclude);
	}
}
