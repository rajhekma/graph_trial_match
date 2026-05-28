import axios from "axios";

const API_BASE = import.meta.env.VITE_API_BASE || "";

// ------------------------------------------------------
// Fetch top results for an NCT (build criteria list)
// ------------------------------------------------------
export function fetchPirResults(nctId) {
  return axios
    .get(`${API_BASE}/api/nct/${encodeURIComponent(nctId)}/results`)
    .then(r => r.data);
}

// ------------------------------------------------------
// Fetch single INCLUSION cluster
// ------------------------------------------------------
export function fetchInclusionCluster(nctId, criteriaIndex) {
  return axios
    .get(`${API_BASE}/api/nct/${encodeURIComponent(nctId)}/inclusion/${criteriaIndex}`)
    .then(r => r.data);
}

// ------------------------------------------------------
// Fetch single EXCLUSION cluster
// ------------------------------------------------------
export function fetchExclusionCluster(nctId, criteriaIndex) {
  return axios
    .get(`${API_BASE}/api/nct/${encodeURIComponent(nctId)}/exclusion/${criteriaIndex}`)
    .then(r => r.data);
}

// ------------------------------------------------------
// Fetch ALL inclusions (multi-ring view)
// ------------------------------------------------------
export function fetchAllInclusions(nctId) {
  return axios
    .get(`${API_BASE}/api/nct/${encodeURIComponent(nctId)}/all_inclusions`)
    .then(r => r.data);
}

// ------------------------------------------------------
// Fetch ALL exclusions (multi-ring view)
// ------------------------------------------------------
export function fetchAllExclusions(nctId) {
  return axios
    .get(`${API_BASE}/api/nct/${encodeURIComponent(nctId)}/all_exclusions`)
    .then(r => r.data);
}

// ------------------------------------------------------
// Neo4j expansion (circles mode / on-demand)
// ------------------------------------------------------
export async function expandNodes(items) {
  if (!Array.isArray(items)) return [];
  const res = await axios.post(`${API_BASE}/api/expand/nodes`, { items });
  return res.data; // [{ center: {...} }, ...]
}
