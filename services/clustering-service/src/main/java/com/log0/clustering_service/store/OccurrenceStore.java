package com.log0.clustering_service.store;

import com.log0.clustering_service.model.ClusterKey;
import com.log0.clustering_service.model.OccurrenceWindow;

public interface OccurrenceStore {
    OccurrenceWindow increment(ClusterKey key, String message);
}
