from scripts import stress_test_departments


def test_reduced_workload_verifies_only_shared_prims_it_touches():
    assert stress_test_departments._shared_prims_to_verify(2, 1) == [
        "/World/Hero",
        "/World/Camera",
    ]
    assert stress_test_departments._shared_prims_to_verify(100, 1) == (
        stress_test_departments.SHARED_PRIMS
    )
    assert stress_test_departments._shared_prims_to_verify(2, 0) == []
