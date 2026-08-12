// Copyright OpenUSDConnect Contributors. All Rights Reserved.

#if WITH_DEV_AUTOMATION_TESTS

#include "SyncClient.h"
#include "Misc/AutomationTest.h"

IMPLEMENT_SIMPLE_AUTOMATION_TEST(
	FOpenUSDConnectReceiverAppliedCursorTest,
	"OpenUSDConnect.Receiver.AppliedCursorIsMonotonicAndResettable",
	EAutomationTestFlags::EditorContext | EAutomationTestFlags::EngineFilter)

bool FOpenUSDConnectReceiverAppliedCursorTest::RunTest(const FString& Parameters)
{
	FSyncClient Client(
		nullptr,
		TEXT("127.0.0.1"),
		7200,
		TEXT(""),
		TEXT("client-a"),
		TEXT("session-a"),
		0.01f,
		41);

	TestEqual(TEXT("reconnect starts from the last successfully applied sequence"),
		Client.GetLastAppliedSeq(), 41);
	Client.MarkAppliedThrough(42);
	TestEqual(TEXT("successful game-thread application advances the cursor"),
		Client.GetLastAppliedSeq(), 42);
	Client.MarkAppliedThrough(40);
	TestEqual(TEXT("an older observation cannot move the cursor backward"),
		Client.GetLastAppliedSeq(), 42);
	Client.ResetAppliedProgress();
	TestEqual(TEXT("an explicit server resync resets the applied cursor"),
		Client.GetLastAppliedSeq(), 0);
	return true;
}

#endif // WITH_DEV_AUTOMATION_TESTS
