// Copyright OpenUSDConnect Contributors. All Rights Reserved.

using UnrealBuildTool;
using System.IO;

public class OpenUSDConnect : ModuleRules
{
	public OpenUSDConnect(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"Sockets",        // FSocket, ISocketSubsystem
			"Networking",     // FInternetAddr
			"DeveloperSettings",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"USDStage",         // AUsdStageActor, UUsdPrimLinkCache
			"USDClasses",       // UUsdAssetCache3
			"USDUtilities",     // UsdToUnreal::ConvertMaterial, FUsdPrimLinkCache
			"UnrealUSDWrapper", // FUsdListener + pxr SDK propagation
			"RHI",              // GMaxRHIShaderPlatform (FMaterialUpdateContext default arg)
		});

		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.Add("UnrealEd"); // FScopedTransaction for USD notice flushing
		}

		// Call the engine helper that configures USD SDK linkage, RTTI, exceptions,
		// memory overload definitions, and USE_USD_SDK macros for this module.
		// This is the same pattern USDStage uses.
		UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);

		// FlatBuffers runtime headers (header-only). Always the plugin-local
		// vendored copy: the flatc-generated bindings under Private/Schema pin
		// the exact runtime version they were produced with (static_assert),
		// so whatever version an engine happens to ship cannot be trusted to
		// match. setup_flatbuffers.py fetches the pinned version.
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
