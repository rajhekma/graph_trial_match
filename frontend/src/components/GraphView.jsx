// src/components/GraphView.jsx
import React, { useRef, useEffect } from "react";
import CytoscapeComponent from "react-cytoscapejs";

const DEFAULT_STYLE = {
  width: "100%",
  height: "780px",
  border: "1px solid #ddd",
  borderRadius: 8,
  background: "#fff",
  minWidth: 0,
  position: "relative"
};

function deg2rad(deg) {
  return (deg * Math.PI) / 180;
}

// ---- CONFIG ----
const LABELS_PER_RING = 20;
const PATIENTS_PER_RING = 25;

const INCLUSION_COLORS = [
  "#2b7a0b", "#0077cc", "#cc0077", "#f39c12", "#8e44ad",
  "#16a085", "#e74c3c", "#34495e", "#d35400", "#2980b9"
];

// consistent color per label type
const LABEL_COLOR_MAP = {
  Condition: "#1f77b4",
  Observation: "#2ca02c",
  Allergy: "#9467bd",
  Medication: "#ff7f0e",
  Encounter: "#17becf",
  Procedure: "#8c564b",
  Default: "#555"
};

export default function GraphView({ cyData, style = DEFAULT_STYLE, onNodeClick, emptyHint }) {
  const cyRef = useRef(null);
  const hasNodes = cyData?.nodes?.length > 0;
  const hint =
    emptyHint ||
    "Select a criterion, click Show All Inclusions/Exclusions, or Show Inclusion/Exclusion Circles.";

  // Helper: rings split
  function makeRings(nodes, perRing, baseRadius, gap) {
    const rings = [];
    let ring = [];
    nodes.forEach((n, i) => {
      if (ring.length === perRing) {
        rings.push(ring);
        ring = [];
      }
      ring.push(n);
    });
    if (ring.length) rings.push(ring);

    return rings.map((ringNodes, i) => ({
      nodes: ringNodes,
      radius: baseRadius + i * gap
    }));
  }

  // Core layout: compute elements for Cytoscape
  function computeElements(nodes, edges) {
    if (!nodes || !nodes.length) return [];

    // Filter expected types
    nodes = nodes.filter(
      n => n && (n.type === "label" || n.type === "patient" || n.type === "label_center" || n.type === "label_instance")
    );

    const container = cyRef.current?.container();
    const width = container?.clientWidth;
    const height = container?.clientHeight;
    if (!width || !height) return [];

    const cx = width / 2;
    const cy = height / 2;

    const normalizedNodes = nodes.map(n => ({
      ...n,
      id: String(n.id),
      props: n.props || {}
    }));

    const normalizedEdges = (edges || []).map(e => ({
      ...e,
      source: String(e.source),
      target: String(e.target)
    }));

    const elems = [];

    // ---------- circlesMode: we *still* add centers to the graph, BUT we provide a top scroll strip in UI.
    // Layout here ensures centers are spaced reasonably (minSpacing), but the visual "control" is the scroll strip.
    if (cyData?.circlesMode) {
      const centers = normalizedNodes.filter(n => n.type === "label_center");

      // Visual constraints & wrapping fallback (but with scroll strip we aim single-row)
      const minSpacing = 140;            // minimum horizontal spacing between centers
      const leftRightPadding = 40;
      const availableW = Math.max(200, width - leftRightPadding * 2);

      // Place centers in a single logical row but ensure min spacing
      const requiredWidth = Math.max(availableW, centers.length * minSpacing);
      const spacing = requiredWidth / (centers.length + 1);

      const topY = Math.max(40, height * 0.12);

      // dedupe helper
      const pushIfNew = el => {
        if (!elems.some(e => e.data && e.data.id === el.data.id)) elems.push(el);
      };

      centers.forEach((c, idx) => {
        // place centers evenly across requiredWidth (they might go beyond visible width => user scrolls the strip)
        const x = leftRightPadding + spacing * (idx + 1);
        const col =
          LABEL_COLOR_MAP[c.label] ||
          LABEL_COLOR_MAP[c.props?.labelName] ||
          LABEL_COLOR_MAP.Default;

        pushIfNew({
          data: {
            id: String(c.id),
            label: c.label,
            type: "label_center",
            props: { ...c.props, _color: col }
          },
          position: { x, y: topY }
        });
      });

      // Attach edges (deduped)
      normalizedEdges.forEach(e => {
        pushIfNew({
          data: {
            id: e.id || `e_${e.source}_${e.target}`,
            source: e.source,
            target: e.target,
            type: e.type
          }
        });
      });

      // Map center positions
      const centerPos = {};
      elems.forEach(el => {
        if (el.data && el.data.type === "label_center") centerPos[el.data.id] = el.position;
      });

      // Instances grouped by parent_center
      const instanceGroups = {};
      normalizedNodes
        .filter(n => n.type === "label_instance")
        .forEach(inst => {
          const parent = inst.props?.parent_center;
          if (!parent) return;
          instanceGroups[parent] = instanceGroups[parent] || [];
          if (!instanceGroups[parent].some(x => x.id === inst.id)) instanceGroups[parent].push(inst);
        });

      // Place instances around parent center
      Object.entries(instanceGroups).forEach(([parentId, instList]) => {
        const pos = centerPos[parentId];
        if (!pos) return;

        // adaptive radius
        const radius = Math.max(120, 110 + instList.length * 4);

        instList.forEach((inst, i) => {
          const id = String(inst.id);
          if (elems.some(e => e.data && e.data.id === id)) return;

          const angle = deg2rad((360 * i) / Math.max(1, instList.length));
          pushIfNew({
            data: {
              id,
              label: inst.label,
              type: "label_instance",
              props: inst.props || {}
            },
            position: {
              x: pos.x + radius * Math.cos(angle),
              y: pos.y + radius * Math.sin(angle)
            }
          });
        });
      });

      // ---------------------------
      // NEW: add patient nodes + edges in circlesMode
      // ---------------------------
      const patientNodesAll = normalizedNodes.filter(n => n.type === "patient");
      if (patientNodesAll.length > 0) {
        // place patients on a lower ring so they don't overlap the top centers/instances
        const patientRadius = Math.max(160, Math.min(width, height) * 0.25);
        const patientCenterY = Math.max(cy, topY + 220); // push patients lower on the canvas
        patientNodesAll.forEach((p, i) => {
          const angle = deg2rad((360 * i) / Math.max(1, patientNodesAll.length));
          const pid = String(p.id);
          if (!elems.some(e => e.data && e.data.id === pid)) {
            pushIfNew({
              data: { id: pid, label: p.label, type: "patient", props: p.props },
              position: { x: cx + patientRadius * Math.cos(angle), y: patientCenterY + patientRadius * Math.sin(angle) }
            });
          }
        });
      }

      // Add edges (dedupe) - include instance->patient and label_center->instance edges already handled above, but ensure all edges present
      normalizedEdges.forEach(e => {
        const eid = e.id || `e_${e.source}_${e.target}`;
        if (!elems.some(x => x.data && x.data.id === eid)) {
          pushIfNew({
            data: {
              id: eid,
              source: e.source,
              target: e.target,
              type: e.type
            }
          });
        }
      });

      return elems;
    }

    // ===================== MULTI / SINGLE inclusion modes (unchanged behavior, with dedupe)
    const labelNodesAll = normalizedNodes.filter(n => n.type === "label");
    const patientNodesAll = normalizedNodes.filter(n => n.type === "patient");

    const inclusionIndices = new Set(
      labelNodesAll
        .map(n => (n.props && (n.props.inclusion_index ?? n.props.criteria_index)) ?? null)
        .filter(x => x !== null)
    );

    const isMulti = !!cyData?.multi || inclusionIndices.size > 1;

    if (isMulti) {
      const indices = Array.from(inclusionIndices).map(Number).sort((a, b) => a - b);

      const labelByIndex = {};
      labelNodesAll.forEach(l => {
        const ii = Number(l.props?.inclusion_index ?? l.props?.criteria_index ?? 0);
        if (!labelByIndex[ii]) labelByIndex[ii] = [];
        labelByIndex[ii].push(l);
      });

      const minDim = Math.min(width, height);
      const base = Math.max(80, minDim * 0.1);
      const gap = Math.max(80, minDim * 0.09);
      const outerRadius = base + gap * indices.length + Math.max(120, minDim * 0.12);

      // labels
      indices.forEach((ii, ringPos) => {
        const ringNodes = labelByIndex[ii] || [];
        const radius = base + ringPos * gap;
        const ringColor = INCLUSION_COLORS[ringPos % INCLUSION_COLORS.length];

        ringNodes.forEach((l, i) => {
          const angle = deg2rad((360 * i) / Math.max(1, ringNodes.length));
          const display = l.props?.text || l.label || "";

          // dedupe push
          if (!elems.some(e => e.data && e.data.id === l.id)) {
            elems.push({
              data: {
                id: l.id,
                label: display,
                type: "label",
                props: { ...l.props, _color: ringColor }
              },
              position: { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) }
            });
          }
        });
      });

      // patients (dedupe)
      patientNodesAll.forEach((p, i) => {
        const angle = (2 * Math.PI * i) / Math.max(1, patientNodesAll.length);
        const pid = String(p.id);
        if (!elems.some(e => e.data && e.data.id === pid)) {
          elems.push({
            data: { id: pid, label: p.label, type: "patient", props: p.props },
            position: { x: cx + outerRadius * Math.cos(angle), y: cy + outerRadius * Math.sin(angle) }
          });
        }
      });

      // edges (dedupe)
      normalizedEdges.forEach(e => {
        const eid = e.id || `e_${e.source}_${e.target}`;
        if (!elems.some(x => x.data && x.data.id === eid)) {
          elems.push({
            data: {
              id: eid,
              source: e.source,
              target: e.target,
              type: e.type
            }
          });
        }
      });

      return elems;
    }

    // Single-inclusion behavior (dedupe)
    const minDim2 = Math.min(width, height);
    const baseLabelRadius = minDim2 * 0.18;
    const labelRingGap = minDim2 * 0.07;

    const basePatientRadius =
      baseLabelRadius +
      labelRingGap * Math.ceil(labelNodesAll.length / LABELS_PER_RING) +
      minDim2 * 0.12;

    const patientRingGap = minDim2 * 0.06;

    const labelRings = makeRings(labelNodesAll, LABELS_PER_RING, baseLabelRadius, labelRingGap);
    const patientRings = makeRings(patientNodesAll, PATIENTS_PER_RING, basePatientRadius, patientRingGap);

    labelRings.forEach(r => {
      r.nodes.forEach((l, i) => {
        const angle = deg2rad((360 * i) / Math.max(1, r.nodes.length));
        const display = l.props?.text || l.label || "";
        if (!elems.some(e => e.data && e.data.id === l.id)) {
          elems.push({
            data: { id: l.id, label: display, type: "label", props: l.props },
            position: { x: cx + r.radius * Math.cos(angle), y: cy + r.radius * Math.sin(angle) }
          });
        }
      });
    });

    patientRings.forEach(r => {
      r.nodes.forEach((p, i) => {
        const angle = deg2rad((360 * i) / Math.max(1, r.nodes.length));
        const pid = String(p.id);
        if (!elems.some(e => e.data && e.data.id === pid)) {
          elems.push({
            data: { id: pid, label: p.label, type: "patient", props: p.props },
            position: { x: cx + r.radius * Math.cos(angle), y: cy + r.radius * Math.sin(angle) }
          });
        }
      });
    });

    normalizedEdges.forEach(e => {
      const eid = e.id || `e_${e.source}_${e.target}`;
      if (!elems.some(x => x.data && x.data.id === eid)) {
        elems.push({
          data: {
            id: eid,
            source: e.source,
            target: e.target,
            type: e.type
          }
        });
      }
    });

    return elems;
  }

  // -----------------------------------------------------------
  // CYTOSCAPE HOOK
  // -----------------------------------------------------------
  function renderGraph(cy) {
    cy.elements().remove();

    const elements = computeElements(cyData.nodes || [], cyData.edges || []);
    if (!elements.length) return false;

    cy.add(elements);

    // STYLE
    cy.style()
      .selector('node[type="label"], node[type="label_center"]')
      .style({
        "background-color": ele => ele.data("props")?._color || "#2b7a0b",
        color: "#fff",
        width: ele => (ele.data("type") === "label_center" ? 80 : 20),
        height: ele => (ele.data("type") === "label_center" ? 80 : 20),
        "border-width": ele => (ele.data("type") === "label_center" ? 4 : 0),
        "border-color": "#000",
        "text-valign": "center",
        "text-halign": "center",
        "font-size": ele => (ele.data("type") === "label_center" ? 13 : 7)
      })
      .selector('node[type="label_instance"]')
      .style({
        "background-color": "#4caf50",
        color: "#fff",
        width: 16,
        height: 16,
        "text-valign": "center",
        "text-halign": "center",
        "font-size": 7
      })
      .selector('node[type="patient"]')
      .style({
        "background-color": "#ff9800",
        color: "#000",
        width: 23,
        height: 23,
        "text-valign": "center",
        "text-halign": "center",
        "font-size": 7
      })
      .selector('edge[type="instance_of"]')
      .style({
        "line-color": "#777",
        width: 2,
        "curve-style": "straight"
      })
      .selector("edge")
      .style({
        "line-color": "#bbb",
        width: 1,
        "curve-style": "bezier",
        opacity: 0.9
      })
      .update();

    cy.layout({ name: "preset" }).run();
    cy.center();
    return true;
  }

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    let mounted = true;

    const apply = () => {
      if (!mounted) return;
      if (!renderGraph(cy)) {
        requestAnimationFrame(() => {
          if (mounted) renderGraph(cy);
        });
      }
    };

    apply();

    const container = cy.container();
    let ro;
    if (container && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => apply());
      ro.observe(container);
    }

    // TOOLTIP
    cy.on("mouseover", "node", evt => {
      const node = evt.target;
      const props = node.data("props") || {};
      const tooltip = document.getElementById("graph-tooltip");

      const html = Object.entries(props)
        .map(([k, v]) => {
          let display = typeof v === "object" ? JSON.stringify(v) : String(v);
          if (display.length > 300) display = display.slice(0, 300) + "…";
          return `<div><strong>${k}</strong>: ${display}</div>`;
        })
        .join("");

      tooltip.innerHTML = html || "<div><em>(no properties)</em></div>";
      tooltip.style.display = "block";
      const pos = evt.renderedPosition;
      tooltip.style.left = pos.x + 20 + "px";
      tooltip.style.top = pos.y + 20 + "px";
    });

    cy.on("mouseout", "node", () => {
      document.getElementById("graph-tooltip").style.display = "none";
    });

    // CLICK -> notify parent
    cy.on("tap", "node", evt => {
      if (typeof onNodeClick === "function") onNodeClick(evt.target.data());
    });

    // DRAG/BOUNCE
    cy.on("free", "node", evt => {
      const node = evt.target;
      node.animate(
        {
          position: {
            x: node.position("x") + (Math.random() * 4 - 2),
            y: node.position("y") + (Math.random() * 4 - 2)
          }
        },
        { duration: 120, easing: "ease-out-quad" }
      );
    });

    return () => {
      mounted = false;
      if (ro) ro.disconnect();
      try { cy.removeAllListeners(); } catch {}
    };
  }, [cyData, onNodeClick]);

  // Extract centers for the scroll strip (purely visual control)
  const centersForStrip = (cyData?.nodes || []).filter(n => n.type === "label_center");

  return (
    <div style={{ position: "relative", ...style }}>
      {/* HORIZONTAL SCROLL STRIP (Option C) */}
      {centersForStrip && centersForStrip.length > 0 && (
        <div
          style={{
            position: "absolute",
            left: 8,
            right: 8,
            top: 8,
            height: 84,
            display: "flex",
            overflowX: "auto",
            gap: 12,
            alignItems: "center",
            padding: "8px 12px",
            zIndex: 20,
            background: "rgba(255,255,255,0.95)",
            borderRadius: 8,
            boxShadow: "0 2px 6px rgba(0,0,0,0.06)"
          }}
        >
          {centersForStrip.map((c, idx) => {
            const col = LABEL_COLOR_MAP[c.label] || LABEL_COLOR_MAP[c.props?.labelName] || LABEL_COLOR_MAP.Default;
            const onClick = () => {
              try {
                if (typeof onNodeClick === "function") {
                  // Mirror the data shape App.jsx expects
                  onNodeClick({
                    id: c.id,
                    type: "label_center",
                    label: c.label,
                    props: c.props
                  });
                }
              } catch (e) {
                console.error("strip onNodeClick failed", e);
              }
            };

            return (
              <div
                key={String(c.id) + "_" + idx}
                onClick={onClick}
                style={{
                  minWidth: 150,
                  padding: "10px 16px",
                  borderRadius: 10,
                  background: col,
                  color: "#fff",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                  boxShadow: "0 2px 6px rgba(0,0,0,0.08)",
                  userSelect: "none"
                }}
                title={String(c.label)}
              >
                <div style={{ fontSize: 14, fontWeight: 700 }}>{c.label}</div>
                {c.props?.labelType && (
                  <div style={{ fontSize: 11, opacity: 0.9, marginTop: 6 }}>
                    {c.props.labelType === "I" ? "Inclusion" : c.props.labelType === "E" ? "Exclusion" : ""}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Cytoscape canvas */}
      <CytoscapeComponent
        elements={[]}
        cy={cy => (cyRef.current = cy)}
        style={{ width: "100%", height: "100%" }}
      />

      <div
        id="graph-tooltip"
        style={{
          position: "absolute",
          display: "none",
          background: "#fff",
          border: "1px solid #ccc",
          padding: "6px 10px",
          borderRadius: 6,
          pointerEvents: "none",
          zIndex: 30,
          fontSize: "12px",
          maxWidth: "420px",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word"
        }}
      ></div>

      {!hasNodes && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#999"
          }}
        >
          {hint}
          {(cyData?.nodes?.length > 0) && (
            <div style={{ marginTop: 8, fontSize: 12, maxWidth: 360, textAlign: "center" }}>
              Graph data loaded but layout is empty — matched concepts may be missing in MySQL. Re-run POST /test_engine after /generate_json.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
