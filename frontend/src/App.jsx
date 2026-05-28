import React, { useState, useEffect } from "react";
import {
  fetchPirResults,
  fetchInclusionCluster,
  fetchExclusionCluster,
  fetchAllInclusions,
  fetchAllExclusions,
  expandNodes
} from "./api";
import CriteriaList from "./components/CriteriaList";
import GraphView from "./components/GraphView";

function buildCriteriaFromRecords(records) {
  const map = {};
  records.forEach(r => {
    const idx = Number(r.criteria_index);
    if (!map[idx]) {
      map[idx] = {
        criteria_index: idx,
        criteria_text: r.criteria_text,
      };
    }
  });
  return Object.values(map).sort((a, b) => a.criteria_index - b.criteria_index);
}

function uniqBy(arr, keyFn) {
  const seen = new Set();
  const out = [];
  arr.forEach(x => {
    const k = keyFn ? keyFn(x) : x;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(x);
    }
  });
  return out;
}

export default function App() {
  const [nct, setNct] = useState("");
  const [criteria, setCriteria] = useState([]);
  const [selectedCrit, setSelectedCrit] = useState(null);
  const [graphData, setGraphData] = useState({ nodes: [], edges: [] });
  const [pirRecords, setPirRecords] = useState([]);
  const [circlesMode, setCirclesMode] = useState(false);
  const [mode, setMode] = useState("I"); // "I" | "E"

  async function loadNct() {
    if (!nct.trim()) return alert("Enter an NCT ID");
    try {
      const data = await fetchPirResults(nct);
      const recs = Array.isArray(data.records) ? data.records : [];
      setPirRecords(recs);
      setSelectedCrit(null);
      setGraphData({ nodes: [], edges: [] });
      setCirclesMode(false);
      if (recs.length === 0) alert("No records returned for this NCT");
    } catch (err) {
      console.error("loadNct failed", err);
      alert("Failed to load NCT results");
    }
  }

  useEffect(() => {
    if (!pirRecords.length || !nct.trim()) return;

    const filtered = pirRecords.filter(
      r => (r.ie || "").trim().toUpperCase() === mode
    );

    const built = buildCriteriaFromRecords(filtered);
    setCriteria(built);
    setSelectedCrit(null);
    setCirclesMode(false);

    // Auto-load graph after Load / mode switch (Load alone only fills the list)
    (async () => {
      try {
        const resp =
          mode === "I"
            ? await fetchAllInclusions(nct)
            : await fetchAllExclusions(nct);
        const nodes = resp.nodes || [];
        const edges = resp.edges || [];
        if (nodes.length > 0) {
          setGraphData({ nodes, edges, multi: true });
          return;
        }
        if (built.length > 0) {
          if (mode === "I") await loadInclusion(built[0]);
          else await loadExclusion(built[0]);
        } else {
          setGraphData({ nodes: [], edges: [] });
        }
      } catch (err) {
        console.error("auto graph load failed", err);
        setGraphData({ nodes: [], edges: [] });
      }
    })();
  }, [mode, pirRecords, nct]);

  async function loadInclusion(c) {
    try {
      const resp = await fetchInclusionCluster(nct, c.criteria_index);
      const patients = resp.patients || [];
      const labels = resp.labels || [];
      const expansions = resp.label_expansions || [];

      const nodes = [];
      const edges = [];

      const expansionMap = {};
      expansions.forEach(exp => {
        if (exp?.center?.id) {
          expansionMap[`concept_${exp.center.id}`] = exp.center.props || {};
        }
      });

      labels.forEach(li => {
        const expandedProps = expansionMap[`concept_${li.id}`];
        const props =
          expandedProps && Object.keys(expandedProps).length > 0
            ? expandedProps
            : { matched_node_id: li.id, matched_label: li.label };

        nodes.push({
          id: `concept_${li.id}`,
          type: "label",
          label: li.display || li.label || "",
          props
        });
      });

      const seen = new Set();
      patients.forEach(p => {
        const pid = p.patient_id;
        const patientNodeId = `pat__${pid}`;
        if (!seen.has(pid)) {
          seen.add(pid);
          nodes.push({
            id: patientNodeId,
            type: "patient",
            label: pid,
            props: p
          });
        }
        if (p.matched_node_id) {
          edges.push({
            source: `concept_${p.matched_node_id}`,
            target: patientNodeId,
            type: "match"
          });
        }
      });

      setSelectedCrit(c.criteria_index);
      setGraphData({ nodes, edges, multi: false });
      setCirclesMode(false);
    } catch (err) {
      console.error("loadInclusion failed", err);
      alert("Failed to load inclusion cluster");
    }
  }

  async function loadExclusion(c) {
    try {
      const resp = await fetchExclusionCluster(nct, c.criteria_index);
      const patients = resp.patients || [];
      const labels = resp.labels || [];
      const expansions = resp.label_expansions || [];

      const nodes = [];
      const edges = [];

      const expansionMap = {};
      expansions.forEach(exp => {
        if (exp?.center?.id) {
          expansionMap[`concept_${exp.center.id}`] = exp.center.props || {};
        }
      });

      labels.forEach(li => {
        const expandedProps = expansionMap[`concept_${li.id}`];
        const props =
          expandedProps && Object.keys(expandedProps).length > 0
            ? expandedProps
            : { matched_node_id: li.id, matched_label: li.label };

        nodes.push({
          id: `concept_${li.id}`,
          type: "label",
          label: li.display || li.label || "",
          props
        });
      });

      const seen = new Set();
      patients.forEach(p => {
        const pid = p.patient_id;
        const patientNodeId = `pat__${pid}`;
        if (!seen.has(pid)) {
          seen.add(pid);
          nodes.push({
            id: patientNodeId,
            type: "patient",
            label: pid,
            props: p
          });
        }
        if (p.matched_node_id) {
          edges.push({
            source: `concept_${p.matched_node_id}`,
            target: patientNodeId,
            type: "excluded"
          });
        }
      });

      setSelectedCrit(c.criteria_index);
      setGraphData({ nodes, edges, multi: false });
      setCirclesMode(false);
    } catch (err) {
      console.error("loadExclusion failed", err);
      alert("Failed to load exclusion cluster");
    }
  }

  async function loadAllInclusions() {
    if (!nct.trim()) return alert("Enter an NCT ID");
    try {
      const resp = await fetchAllInclusions(nct);
      setSelectedCrit(null);
      setGraphData({ nodes: resp.nodes || [], edges: resp.edges || [], multi: true });
      setCirclesMode(false);
    } catch (err) {
      console.error("loadAllInclusions failed", err);
      alert("Failed to load all inclusions");
    }
  }

  async function loadAllExclusions() {
    if (!nct.trim()) return alert("Enter an NCT ID");
    try {
      const resp = await fetchAllExclusions(nct);
      setSelectedCrit(null);
      setGraphData({ nodes: resp.nodes || [], edges: resp.edges || [], multi: true });
      setCirclesMode(false);
    } catch (err) {
      console.error("loadAllExclusions failed", err);
      alert("Failed to load all exclusions");
    }
  }

  function buildCirclesFromPirRecords(recs) {
    const incLabels = uniqBy(
      recs.filter(r => (r.ie || "").toUpperCase() === "I").map(r => r.matched_label || ""),
      x => x
    ).filter(Boolean);

    const excLabels = uniqBy(
      recs.filter(r => (r.ie || "").toUpperCase() === "E").map(r => r.matched_label || ""),
      x => x
    ).filter(Boolean);

    const nodes = [];
    const edges = [];

    incLabels.forEach(lab => {
      nodes.push({
        id: `center_inc_${lab}`,
        type: "label_center",
        label: lab,
        props: { labelType: "I", labelName: lab }
      });
    });

    excLabels.forEach(lab => {
      nodes.push({
        id: `center_exc_${lab}`,
        type: "label_center",
        label: lab,
        props: { labelType: "E", labelName: lab }
      });
    });

    return { nodes, edges };
  }

  function handleShowCircles() {
    if (!nct.trim()) return alert("Enter an NCT ID");
    if (!pirRecords.length) {
      alert("Load the NCT first");
      return;
    }
    const gd = buildCirclesFromPirRecords(pirRecords);
    setGraphData({ nodes: gd.nodes, edges: gd.edges, multi: true, circlesMode: true });
    setSelectedCrit(null);
    setCirclesMode(true);
  }

  async function handleNodeClick(data) {
    if (!data || !data.type) return;

    if (data.type === "label_center") {
      const labelName = data.props?.labelName || data.label;
      if (!labelName) return;

      const matches = pirRecords
        .filter(r => (r.matched_label || "") === labelName && r.matched_node_id)
        .map(r => ({ matched_node_id: r.matched_node_id }));

      const uniqMatches = uniqBy(matches, m => m.matched_node_id);
      if (!uniqMatches.length) return;

      const items = uniqMatches.map(m => ({ id: m.matched_node_id, label: labelName }));
      const expanded = await expandNodes(items);

      const instanceNodes = [];
      const instanceEdges = [];

      expanded.forEach((exp, idx) => {
        const uid = items[idx].id;
        const instId = `inst_${uid}`;
        const props = {
          matched_node_id: uid,
          matched_label: labelName,
          parent_center: data.id,
          ...(exp?.center?.props || {})
        };

        instanceNodes.push({
          id: instId,
          type: "label_instance",
          label: uid,
          props
        });

        instanceEdges.push({
          source: data.id,
          target: instId,
          type: "instance_of"
        });
      });

      setGraphData(gd => ({
        ...gd,
        nodes: uniqBy([...gd.nodes, ...instanceNodes], n => n.id),
        edges: uniqBy([...gd.edges, ...instanceEdges], e => `${e.source}-${e.target}-${e.type}`),
        multi: true,
        circlesMode: true
      }));
    }
  }

  return (
    <div style={{ display: "flex", gap: 12, padding: 12 }}>
      <div style={{ width: 360, borderRight: "1px solid #ddd", paddingRight: 12 }}>
        <h3>PIR Demo</h3>

        <input
          placeholder="Enter NCT ID"
          value={nct}
          onChange={e => setNct(e.target.value)}
          style={{ width: "100%", marginBottom: 8 }}
        />
        <button onClick={loadNct} style={{ width: "100%", marginBottom: 8 }}>
          Load
        </button>

        <button onClick={handleShowCircles} style={{ width: "100%", marginBottom: 12 }}>
          Show Inclusion / Exclusion Circles
        </button>

        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <button onClick={() => setMode("I")} style={{ flex: 1 }}>
            Inclusion
          </button>
          <button onClick={() => setMode("E")} style={{ flex: 1 }}>
            Exclusion
          </button>
        </div>

        <CriteriaList
          criteria={criteria}
          selectedIndex={selectedCrit}
          onSelect={mode === "I" ? loadInclusion : loadExclusion}
          title={mode === "I" ? "Inclusion criteria" : "Exclusion criteria"}
        />

        <button
          onClick={mode === "I" ? loadAllInclusions : loadAllExclusions}
          style={{ width: "100%", marginTop: 12 }}
        >
          {mode === "I" ? "Show All Inclusions" : "Show All Exclusions"}
        </button>
      </div>

      <div style={{ flex: 1, minWidth: 0 }}>
        <GraphView
          cyData={graphData}
          onNodeClick={handleNodeClick}
          emptyHint={
            pirRecords.length
              ? "No graph nodes yet — click a criterion, Show All, or Circles. If still empty, run /test_engine for this NCT."
              : "Enter an NCT ID and click Load."
          }
        />
      </div>
    </div>
  );
}
