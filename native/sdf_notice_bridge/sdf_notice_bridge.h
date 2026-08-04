#ifndef OPENUSDCONNECT_SDF_NOTICE_BRIDGE_H
#define OPENUSDCONNECT_SDF_NOTICE_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(OUC_SDF_NOTICE_BRIDGE_BUILD)
#define OUC_SDF_NOTICE_API __declspec(dllexport)
#else
#define OUC_SDF_NOTICE_API __declspec(dllimport)
#endif
#elif defined(__GNUC__) || defined(__clang__)
#define OUC_SDF_NOTICE_API __attribute__((visibility("default")))
#else
#define OUC_SDF_NOTICE_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct OucSdfNoticeTracker OucSdfNoticeTracker;

#define OUC_SDF_NOTICE_ABI_VERSION UINT32_C(1)

#define OUC_SDF_CHANGE_RENAMED (UINT64_C(1) << 0)
#define OUC_SDF_CHANGE_LAYER_IDENTIFIER (UINT64_C(1) << 1)
#define OUC_SDF_CHANGE_LAYER_RESOLVED_PATH (UINT64_C(1) << 2)
#define OUC_SDF_CHANGE_REPLACED_CONTENT (UINT64_C(1) << 3)
#define OUC_SDF_CHANGE_RELOADED_CONTENT (UINT64_C(1) << 4)
#define OUC_SDF_CHANGE_REORDERED_CHILDREN (UINT64_C(1) << 5)
#define OUC_SDF_CHANGE_REORDERED_PROPERTIES (UINT64_C(1) << 6)
#define OUC_SDF_CHANGE_PRIM_VARIANT_SETS (UINT64_C(1) << 7)
#define OUC_SDF_CHANGE_PRIM_INHERITS (UINT64_C(1) << 8)
#define OUC_SDF_CHANGE_PRIM_SPECIALIZES (UINT64_C(1) << 9)
#define OUC_SDF_CHANGE_PRIM_REFERENCES (UINT64_C(1) << 10)
#define OUC_SDF_CHANGE_TIME_SAMPLES (UINT64_C(1) << 11)
#define OUC_SDF_CHANGE_CONNECTIONS (UINT64_C(1) << 12)
#define OUC_SDF_CHANGE_RELATIONSHIP_TARGETS (UINT64_C(1) << 13)
#define OUC_SDF_CHANGE_ADDED_TARGET (UINT64_C(1) << 14)
#define OUC_SDF_CHANGE_REMOVED_TARGET (UINT64_C(1) << 15)
#define OUC_SDF_CHANGE_ADDED_INERT_PRIM (UINT64_C(1) << 16)
#define OUC_SDF_CHANGE_ADDED_PRIM (UINT64_C(1) << 17)
#define OUC_SDF_CHANGE_REMOVED_INERT_PRIM (UINT64_C(1) << 18)
#define OUC_SDF_CHANGE_REMOVED_PRIM (UINT64_C(1) << 19)
#define OUC_SDF_CHANGE_ADDED_DECLARATION (UINT64_C(1) << 20)
#define OUC_SDF_CHANGE_ADDED_PROPERTY (UINT64_C(1) << 21)
#define OUC_SDF_CHANGE_REMOVED_DECLARATION (UINT64_C(1) << 22)
#define OUC_SDF_CHANGE_REMOVED_PROPERTY (UINT64_C(1) << 23)
#define OUC_SDF_CHANGE_SUBLAYERS (UINT64_C(1) << 24)

typedef struct OucSdfNoticeRecord {
    uint64_t serial;
    uint64_t flags;
    const char *layer_identifier;
    size_t layer_identifier_size;
    const char *path;
    size_t path_size;
    const char *old_path;
    size_t old_path_size;
    const char *old_identifier;
    size_t old_identifier_size;
    const char *const *fields;
    size_t field_count;
} OucSdfNoticeRecord;

typedef struct OucSdfNoticeBatch {
    uint64_t serial;
    const OucSdfNoticeRecord *records;
    size_t record_count;
} OucSdfNoticeBatch;

OUC_SDF_NOTICE_API OucSdfNoticeTracker *
ouc_sdf_notice_tracker_create(
    const char *const *layer_identifiers,
    size_t layer_count,
    size_t max_queued_bytes);

OUC_SDF_NOTICE_API void
ouc_sdf_notice_tracker_destroy(OucSdfNoticeTracker *tracker);

OUC_SDF_NOTICE_API int
ouc_sdf_notice_tracker_set_layers(
    OucSdfNoticeTracker *tracker,
    const char *const *layer_identifiers,
    size_t layer_count);

OUC_SDF_NOTICE_API int
ouc_sdf_notice_tracker_set_suppressed(
    OucSdfNoticeTracker *tracker,
    int suppressed);

OUC_SDF_NOTICE_API int
ouc_sdf_notice_tracker_acquire(OucSdfNoticeTracker *tracker, OucSdfNoticeBatch *batch);

OUC_SDF_NOTICE_API int
ouc_sdf_notice_tracker_release(OucSdfNoticeTracker *tracker);

OUC_SDF_NOTICE_API size_t
ouc_sdf_notice_tracker_pending_batches(const OucSdfNoticeTracker *tracker);

OUC_SDF_NOTICE_API size_t
ouc_sdf_notice_tracker_queued_bytes(const OucSdfNoticeTracker *tracker);

OUC_SDF_NOTICE_API uint64_t
ouc_sdf_notice_tracker_coalesced_batch_count(const OucSdfNoticeTracker *tracker);

OUC_SDF_NOTICE_API const char *
ouc_sdf_notice_last_error(void);

OUC_SDF_NOTICE_API uint32_t
ouc_sdf_notice_pxr_version(void);

OUC_SDF_NOTICE_API uint32_t
ouc_sdf_notice_abi_version(void);

#ifdef __cplusplus
}
#endif

#endif
