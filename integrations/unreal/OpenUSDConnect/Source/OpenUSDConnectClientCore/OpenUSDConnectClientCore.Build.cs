// Copyright OpenUSDConnect Contributors. All Rights Reserved.

using System.IO;
using UnrealBuildTool;

// The repository's native/client_core, staged into include/ and src/ before
// BuildPlugin.
public class OpenUSDConnectClientCore : ModuleRules
{
	public OpenUSDConnectClientCore(ReadOnlyTargetRules Target) : base(Target)
	{
		bRequiresImplementModule = false;
		PCHUsage = PCHUsageMode.NoPCHs;
		bUseUnity = false;

		PublicIncludePaths.Add(Path.Combine(ModuleDirectory, "include"));

		// Other modules call into this module's DLL, so the core's API carries
		// the export attribute; Platform.h defines the DLLEXPORT it expands to.
		// Linking Core also gives the DLL Unreal's operator new and delete, which
		// the containers that cross the API to other modules require.
		PublicDefinitions.Add("OPENUSDCONNECT_CLIENT_API=OPENUSDCONNECTCLIENTCORE_API");
		PrivateDependencyModuleNames.Add("Core");
		ForceIncludeFiles.Add("HAL/Platform.h");

		string FlatBuffersInclude = Path.GetFullPath(Path.Combine(
			ModuleDirectory, "..", "OpenUSDConnectPXR", "ThirdParty", "flatbuffers", "include"));
		if (!File.Exists(Path.Combine(FlatBuffersInclude, "flatbuffers", "flatbuffer_builder.h")))
		{
			throw new BuildException(
				"OpenUSDConnect: FlatBuffers headers not found. Run  " +
				"python <plugin>/setup_flatbuffers.py  once, then rebuild.");
		}
		PublicSystemIncludePaths.Add(FlatBuffersInclude);
	}
}
