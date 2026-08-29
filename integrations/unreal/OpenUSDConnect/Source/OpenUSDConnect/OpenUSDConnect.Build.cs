// Copyright OpenUSDConnect Contributors. All Rights Reserved.

using System.IO;
using UnrealBuildTool;

public class OpenUSDConnect : ModuleRules
{
	public OpenUSDConnect(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
		PublicIncludePaths.Add(Path.GetFullPath(Path.Combine(
			ModuleDirectory,
			"../ThirdParty/OpenUSDConnectClientCore/include")));

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
			"OpenUSDConnectPXR", // RTTI-enabled pxr stage bridge and event application
			"USDStage",          // AUsdStageActor
			"UnrealUSDWrapper",  // FUsdListener
		});

		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.Add("UnrealEd"); // FScopedTransaction for USD notice flushing
		}
	}
}
