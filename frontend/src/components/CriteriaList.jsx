// src/components/CriteriaList.jsx
import React from "react";

export default function CriteriaList({ criteria, selectedIndex, onSelect, title = "Criteria" }) {
  return (
    <div style={{ padding: 8 }}>
      <h4>{title}</h4>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {criteria.map(c => (
          <li key={c.criteria_index} style={{ marginBottom: 8 }}>
            <label style={{ cursor: "pointer", display: "block", padding: 6, borderRadius: 6, background: selectedIndex === c.criteria_index ? "#e3f2fd" : "transparent" }}>
              <input type="radio" checked={selectedIndex === c.criteria_index} onChange={() => onSelect(c)} />
              <strong style={{ marginLeft: 8 }}>{c.criteria_index}</strong> – <span style={{ marginLeft: 6 }}>{c.criteria_text}</span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  );
}
