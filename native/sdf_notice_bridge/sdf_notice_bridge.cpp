#include "sdf_notice_bridge.h"

#include "pxr/pxr.h"
#include "pxr/base/tf/notice.h"
#include "pxr/base/tf/weakBase.h"
#include "pxr/base/tf/weakPtr.h"
#include "pxr/usd/sdf/changeList.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/notice.h"

#include <algorithm>
#include <deque>
#include <mutex>
#include <new>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

PXR_NAMESPACE_USING_DIRECTIVE

namespace {

thread_local std::string lastError;

void
SetError(const char *message)
{
    lastError = message;
}

struct OwnedRecord {
    uint64_t serial = 0;
    uint64_t flags = 0;
    std::string layerIdentifier;
    std::string path;
    std::string oldPath;
    std::string oldIdentifier;
    std::vector<std::string> fields;
    std::vector<const char *> fieldViews;

    void AddField(const std::string &field)
    {
        if (std::find(fields.begin(), fields.end(), field) == fields.end()) {
            fields.push_back(field);
        }
    }

    void Finalize()
    {
        fieldViews.reserve(fields.size());
        for (const std::string &field : fields) {
            fieldViews.push_back(field.c_str());
        }
    }

    size_t ByteSize() const
    {
        size_t result = sizeof(*this) + layerIdentifier.size() + path.size() +
            oldPath.size() + oldIdentifier.size();
        result += fieldViews.size() * sizeof(const char *);
        for (const std::string &field : fields) {
            result += sizeof(field) + field.size();
        }
        return result;
    }
};

struct OwnedBatch {
    uint64_t serial = 0;
    std::vector<OwnedRecord> records;
    std::vector<OucSdfNoticeRecord> views;
    size_t byteSize = 0;

    void Finalize()
    {
        views.reserve(records.size());
        byteSize = sizeof(*this);
        for (OwnedRecord &record : records) {
            record.Finalize();
            byteSize += record.ByteSize() + sizeof(OucSdfNoticeRecord);
            views.push_back({
                record.serial,
                record.flags,
                record.layerIdentifier.c_str(),
                record.layerIdentifier.size(),
                record.path.c_str(),
                record.path.size(),
                record.oldPath.c_str(),
                record.oldPath.size(),
                record.oldIdentifier.c_str(),
                record.oldIdentifier.size(),
                record.fieldViews.data(),
                record.fieldViews.size(),
            });
        }
    }
};

OwnedBatch
MakeReplacementBatch(
    uint64_t serial,
    const std::unordered_set<std::string> &layerIdentifiers)
{
    std::vector<std::string> identifiers(
        layerIdentifiers.begin(), layerIdentifiers.end());
    std::sort(identifiers.begin(), identifiers.end());

    OwnedBatch batch;
    batch.serial = serial;
    batch.records.reserve(identifiers.size());
    for (const std::string &identifier : identifiers) {
        OwnedRecord record;
        record.serial = serial;
        record.flags = OUC_SDF_CHANGE_REPLACED_CONTENT |
            OUC_SDF_CHANGE_SUBLAYERS;
        record.layerIdentifier = identifier;
        record.path = "/";
        batch.records.push_back(std::move(record));
    }
    batch.Finalize();
    return batch;
}

uint64_t
GetFlags(const SdfChangeList::Entry &entry)
{
    const auto &flags = entry.flags;
    uint64_t result = 0;
    result |= flags.didRename ? OUC_SDF_CHANGE_RENAMED : 0;
    result |= flags.didChangeIdentifier ? OUC_SDF_CHANGE_LAYER_IDENTIFIER : 0;
    result |= flags.didChangeResolvedPath ? OUC_SDF_CHANGE_LAYER_RESOLVED_PATH : 0;
    result |= flags.didReplaceContent ? OUC_SDF_CHANGE_REPLACED_CONTENT : 0;
    result |= flags.didReloadContent ? OUC_SDF_CHANGE_RELOADED_CONTENT : 0;
    result |= flags.didReorderChildren ? OUC_SDF_CHANGE_REORDERED_CHILDREN : 0;
    result |= flags.didReorderProperties ? OUC_SDF_CHANGE_REORDERED_PROPERTIES : 0;
    result |= flags.didChangePrimVariantSets ? OUC_SDF_CHANGE_PRIM_VARIANT_SETS : 0;
    result |= flags.didChangePrimInheritPaths ? OUC_SDF_CHANGE_PRIM_INHERITS : 0;
    result |= flags.didChangePrimSpecializes ? OUC_SDF_CHANGE_PRIM_SPECIALIZES : 0;
    result |= flags.didChangePrimReferences ? OUC_SDF_CHANGE_PRIM_REFERENCES : 0;
    result |= flags.didChangeAttributeTimeSamples ? OUC_SDF_CHANGE_TIME_SAMPLES : 0;
    result |= flags.didChangeAttributeConnection ? OUC_SDF_CHANGE_CONNECTIONS : 0;
    result |= flags.didChangeRelationshipTargets ? OUC_SDF_CHANGE_RELATIONSHIP_TARGETS : 0;
    result |= flags.didAddTarget ? OUC_SDF_CHANGE_ADDED_TARGET : 0;
    result |= flags.didRemoveTarget ? OUC_SDF_CHANGE_REMOVED_TARGET : 0;
    result |= flags.didAddInertPrim ? OUC_SDF_CHANGE_ADDED_INERT_PRIM : 0;
    result |= flags.didAddNonInertPrim ? OUC_SDF_CHANGE_ADDED_PRIM : 0;
    result |= flags.didRemoveInertPrim ? OUC_SDF_CHANGE_REMOVED_INERT_PRIM : 0;
    result |= flags.didRemoveNonInertPrim ? OUC_SDF_CHANGE_REMOVED_PRIM : 0;
    result |= flags.didAddPropertyWithOnlyRequiredFields
        ? OUC_SDF_CHANGE_ADDED_DECLARATION
        : 0;
    result |= flags.didAddProperty ? OUC_SDF_CHANGE_ADDED_PROPERTY : 0;
    result |= flags.didRemovePropertyWithOnlyRequiredFields
        ? OUC_SDF_CHANGE_REMOVED_DECLARATION
        : 0;
    result |= flags.didRemoveProperty ? OUC_SDF_CHANGE_REMOVED_PROPERTY : 0;
    result |= entry.subLayerChanges.empty() ? 0 : OUC_SDF_CHANGE_SUBLAYERS;
    return result;
}

} // namespace

struct OucSdfNoticeTracker : public TfWeakBase {
    explicit OucSdfNoticeTracker(
        const char *const *layerIdentifiers,
        size_t layerCount,
        size_t maxBytes)
        : trackAllLayers(layerCount == 0)
        , maxQueuedBytes(maxBytes)
    {
        SetLayers(layerIdentifiers, layerCount);
        noticeKey = TfNotice::Register(
            TfCreateWeakPtr(this),
            &OucSdfNoticeTracker::OnLayersDidChange);
    }

    ~OucSdfNoticeTracker()
    {
        TfNotice::RevokeAndWait(noticeKey);
    }

    void SetLayers(const char *const *layerIdentifiers, size_t layerCount)
    {
        std::unordered_set<std::string> identifiers;
        for (size_t index = 0; index != layerCount; ++index) {
            if (layerIdentifiers[index]) {
                identifiers.insert(layerIdentifiers[index]);
            }
        }
        trackedLayers = std::move(identifiers);
        trackAllLayers = layerCount == 0;
    }

    void Queue(OwnedBatch batch)
    {
        batch.Finalize();
        if (!maxQueuedBytes || queuedBytes + batch.byteSize <= maxQueuedBytes) {
            queuedBytes += batch.byteSize;
            batches.push_back(std::move(batch));
            return;
        }

        std::unordered_set<std::string> replacementLayers;
        uint64_t replacementSerial = batch.serial;
        const size_t retained = acquired ? 1 : 0;
        for (auto iterator = batches.begin() + retained; iterator != batches.end(); ++iterator) {
            replacementSerial = std::max(replacementSerial, iterator->serial);
            for (const OwnedRecord &record : iterator->records) {
                replacementLayers.insert(record.layerIdentifier);
            }
            queuedBytes -= iterator->byteSize;
        }
        batches.erase(batches.begin() + retained, batches.end());
        for (const OwnedRecord &record : batch.records) {
            replacementLayers.insert(record.layerIdentifier);
        }

        OwnedBatch replacement =
            MakeReplacementBatch(replacementSerial, replacementLayers);
        queuedBytes += replacement.byteSize;
        batches.push_back(std::move(replacement));
        ++coalescedBatchCount;
    }

    void OnLayersDidChange(const SdfNotice::LayersDidChange &notice) noexcept
    {
        try {
            CaptureChanges(notice);
        }
        catch (...) {
            std::lock_guard<std::mutex> lock(mutex);
            callbackFailed = true;
        }
    }

    void CaptureChanges(const SdfNotice::LayersDidChange &notice)
    {
        std::lock_guard<std::mutex> lock(mutex);
        if (suppressed) {
            return;
        }
        OwnedBatch batch;
        batch.serial = notice.GetSerialNumber();

        for (const auto &layerAndChanges : notice.GetChangeListVec()) {
            const SdfLayerHandle &layer = layerAndChanges.first;
            if (!layer) {
                continue;
            }
            const std::string layerIdentifier = layer->GetIdentifier();
            const SdfChangeList &changeList = layerAndChanges.second;
            std::string oldLayerIdentifier;
            for (const auto &pathAndEntry : changeList.GetEntryList()) {
                const SdfChangeList::Entry &entry = pathAndEntry.second;
                if (entry.flags.didChangeIdentifier) {
                    oldLayerIdentifier = entry.oldIdentifier;
                    break;
                }
            }
            if (
                !trackAllLayers &&
                trackedLayers.count(layerIdentifier) == 0 &&
                (oldLayerIdentifier.empty() ||
                 trackedLayers.count(oldLayerIdentifier) == 0)) {
                continue;
            }
            for (const auto &pathAndEntry : changeList.GetEntryList()) {
                const SdfPath &path = pathAndEntry.first;
                const SdfChangeList::Entry &entry = pathAndEntry.second;
                OwnedRecord record;
                record.serial = batch.serial;
                record.flags = GetFlags(entry);
                record.layerIdentifier = layerIdentifier;
                record.path = path.GetString();
                record.oldPath = entry.oldPath.GetString();
                record.oldIdentifier = entry.oldIdentifier;
                record.fields.reserve(entry.infoChanged.size());
                for (const auto &fieldAndValues : entry.infoChanged) {
                    record.AddField(fieldAndValues.first.GetString());
                }
                if (entry.flags.didChangeAttributeTimeSamples) {
                    record.AddField("timeSamples");
                }
                if (entry.flags.didChangeAttributeConnection) {
                    record.AddField("connectionPaths");
                }
                if (entry.flags.didChangeRelationshipTargets) {
                    record.AddField("targetPaths");
                }
                if (entry.flags.didReorderChildren) {
                    record.AddField("primOrder");
                }
                if (entry.flags.didReorderProperties) {
                    record.AddField("propertyOrder");
                }
                if (entry.flags.didChangePrimVariantSets) {
                    record.AddField("variantSetNames");
                }
                if (entry.flags.didChangePrimInheritPaths) {
                    record.AddField("inheritPaths");
                }
                if (entry.flags.didChangePrimSpecializes) {
                    record.AddField("specializes");
                }
                if (entry.flags.didChangePrimReferences) {
                    record.AddField("references");
                }
                batch.records.push_back(std::move(record));
            }
            if (!trackAllLayers && !oldLayerIdentifier.empty()) {
                trackedLayers.erase(oldLayerIdentifier);
                trackedLayers.insert(layerIdentifier);
            }
        }

        if (!batch.records.empty()) {
            Queue(std::move(batch));
        }
    }

    mutable std::mutex mutex;
    std::unordered_set<std::string> trackedLayers;
    bool trackAllLayers = false;
    bool suppressed = false;
    bool callbackFailed = false;
    bool acquired = false;
    size_t maxQueuedBytes = 0;
    size_t queuedBytes = 0;
    uint64_t coalescedBatchCount = 0;
    std::deque<OwnedBatch> batches;
    TfNotice::Key noticeKey;
};

extern "C" {

OucSdfNoticeTracker *
ouc_sdf_notice_tracker_create(
    const char *const *layerIdentifiers,
    size_t layerCount,
    size_t maxQueuedBytes)
{
    lastError.clear();
    if (layerCount != 0 && !layerIdentifiers) {
        SetError("layer identifiers are null");
        return nullptr;
    }
    try {
        return new OucSdfNoticeTracker(
            layerIdentifiers, layerCount, maxQueuedBytes);
    }
    catch (const std::exception &error) {
        lastError = error.what();
    }
    catch (...) {
        SetError("unknown error while creating Sdf notice tracker");
    }
    return nullptr;
}

int
ouc_sdf_notice_tracker_set_layers(
    OucSdfNoticeTracker *tracker,
    const char *const *layerIdentifiers,
    size_t layerCount)
{
    lastError.clear();
    if (!tracker || (layerCount != 0 && !layerIdentifiers)) {
        SetError("tracker and layer identifiers are required");
        return -1;
    }
    try {
        std::lock_guard<std::mutex> lock(tracker->mutex);
        tracker->SetLayers(layerIdentifiers, layerCount);
        return 0;
    }
    catch (const std::exception &error) {
        lastError = error.what();
    }
    catch (...) {
        SetError("unknown error while updating tracked Sdf layers");
    }
    return -1;
}

int
ouc_sdf_notice_tracker_set_suppressed(
    OucSdfNoticeTracker *tracker,
    int suppressed)
{
    lastError.clear();
    if (!tracker) {
        SetError("tracker is required");
        return -1;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    tracker->suppressed = suppressed != 0;
    return 0;
}

void
ouc_sdf_notice_tracker_destroy(OucSdfNoticeTracker *tracker)
{
    delete tracker;
}

int
ouc_sdf_notice_tracker_acquire(
    OucSdfNoticeTracker *tracker,
    OucSdfNoticeBatch *batch)
{
    lastError.clear();
    if (!tracker || !batch) {
        SetError("tracker and batch are required");
        return -1;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    if (tracker->callbackFailed) {
        SetError("Sdf notice callback failed");
        return -1;
    }
    if (tracker->acquired) {
        SetError("a batch is already acquired");
        return -1;
    }
    if (tracker->batches.empty()) {
        *batch = {};
        return 0;
    }
    const OwnedBatch &owned = tracker->batches.front();
    *batch = {owned.serial, owned.views.data(), owned.views.size()};
    tracker->acquired = true;
    return 1;
}

int
ouc_sdf_notice_tracker_release(OucSdfNoticeTracker *tracker)
{
    lastError.clear();
    if (!tracker) {
        SetError("tracker is required");
        return -1;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    if (!tracker->acquired || tracker->batches.empty()) {
        SetError("no batch is acquired");
        return -1;
    }
    tracker->queuedBytes -= tracker->batches.front().byteSize;
    tracker->batches.pop_front();
    tracker->acquired = false;
    return 0;
}

size_t
ouc_sdf_notice_tracker_pending_batches(const OucSdfNoticeTracker *tracker)
{
    if (!tracker) {
        return 0;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    return tracker->batches.size();
}

size_t
ouc_sdf_notice_tracker_queued_bytes(const OucSdfNoticeTracker *tracker)
{
    if (!tracker) {
        return 0;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    return tracker->queuedBytes;
}

uint64_t
ouc_sdf_notice_tracker_coalesced_batch_count(const OucSdfNoticeTracker *tracker)
{
    if (!tracker) {
        return 0;
    }
    std::lock_guard<std::mutex> lock(tracker->mutex);
    return tracker->coalescedBatchCount;
}

const char *
ouc_sdf_notice_last_error(void)
{
    return lastError.c_str();
}

uint32_t
ouc_sdf_notice_pxr_version(void)
{
    return PXR_VERSION;
}

uint32_t
ouc_sdf_notice_abi_version(void)
{
    return OUC_SDF_NOTICE_ABI_VERSION;
}

} // extern "C"
