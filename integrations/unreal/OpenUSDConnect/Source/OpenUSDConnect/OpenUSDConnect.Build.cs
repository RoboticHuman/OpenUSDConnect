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

		// Call the engine helper that configures USD SDK linkage, RTTI, exceptions,
		// memory overload definitions, and USE_USD_SDK macros for this module.
		// This is the same pattern USDStage uses.
		UnrealBuildTool.Rules.UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);

		// FlatBuffers headers (header-only): source engine checkouts ship them
		// under Engine/Source/ThirdParty (version directory varies per engine
		// release); Launcher builds ship only the license stub, so
		// setup_flatbuffers.py fetches the engine's declared version into this
		// plugin's ThirdParty folder.
		string FlatBuffersInclude = ResolveFlatBuffersInclude();
		if (FlatBuffersInclude == null)
		{
			throw new BuildException(
				"OpenUSDConnect: FlatBuffers headers not found. This engine does " +
				"not ship them (Launcher builds carry only the license stub). " +
				"Run  python <plugin>/setup_flatbuffers.py --engine \"" +
				EngineDirectory + "\"  once, then rebuild.");
		}
		PublicSystemIncludePaths.Add(FlatBuffersInclude);
	}

	private string ResolveFlatBuffersInclude()
	{
		string EngineFb = Path.Combine(EngineDirectory, "Source", "ThirdParty", "flatbuffers");
		if (Directory.Exists(EngineFb))
		{
			foreach (string VersionDir in Directory.GetDirectories(EngineFb, "flatbuffers-*"))
			{
				string Include = Path.Combine(VersionDir, "include");
				if (File.Exists(Path.Combine(Include, "flatbuffers", "flatbuffer_builder.h")))
				{
					return Include;
				}
			}
		}

		string LocalInclude = Path.Combine(ModuleDirectory, "ThirdParty", "flatbuffers", "include");
		if (File.Exists(Path.Combine(LocalInclude, "flatbuffers", "flatbuffer_builder.h")))
		{
			return LocalInclude;
		}
		return null;
	}
}
