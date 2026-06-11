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
			"USDStage",         // AUsdStageActor
			"UnrealUSDWrapper", // FUsdListener + pxr SDK propagation
		});

		// Call the engine helper that configures USD SDK linkage, RTTI, exceptions,
		// memory overload definitions, and USE_USD_SDK macros for this module.
		// This is the same pattern USDStage uses.
		UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);

		// FlatBuffers headers — shipped with Unreal Engine
		string FlatBuffersInclude = Path.Combine(
			EngineDirectory,
			"Source", "ThirdParty", "flatbuffers", "flatbuffers-24.3.25", "include");

		if (Directory.Exists(FlatBuffersInclude))
		{
			PublicSystemIncludePaths.Add(FlatBuffersInclude);
		}
	}
}
