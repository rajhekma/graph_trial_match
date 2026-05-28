from openai import OpenAI
import os
from dotenv import load_dotenv
import json
import re

# Load environment variables
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def generate_cypher(criteria_json: dict) -> str:
    """
    Convert structured JSON criteria into a Neo4j Cypher query.
    Uses the refined schema-aware prompt.
    """
    prompt = f"""
You are an expert in converting structured eligibility JSON into Neo4j Cypher queries.  
The Neo4j database contains FHIR-like resources with the following schema:

---
### GRAPH SCHEMA
Nodes:
- Patient(id, birthDate, gender, fieldFamilyName, fieldGivenName, maritalStatus, fieldIdentifierValue, address, telecom, communication)
- Observation(id, status, code, valueQuantity, effectiveDateTime, issued, subject, encounter, category)
- Condition(id, code, verificationStatus, clinicalStatus, onsetDateTime, encounter, subject)
- DiagnosticReport(id, code, category, status, effectiveDateTime, issued, subject, encounter, result)
- MedicationRequest(id, status, intent, authoredOn, subject, encounter, requester, medicationCodeableConcept)
- AllergyIntolerance(id, code, verificationStatus, clinicalStatus, patient, recordedDate, criticality, type, category)
- CodeableConcept(id, text)
- Coding(id, system, code, text, display, head)

Relationships (all undirected):
- (Patient)-[:REFERENCES]-(Observation|Condition|DiagnosticReport|MedicationRequest|AllergyIntolerance)
- (Observation|Condition|DiagnosticReport|MedicationRequest|AllergyIntolerance)-[:REFERENCES]-(CodeableConcept)
- (CodeableConcept)-[:REFERENCES]-(Coding)

---
### RULES FOR QUERY GENERATION
1. Always use undirected edges: `(a)-[:REFERENCES]-(b)`.
2. Respect `"inclusion"` and `"exclusion"` from JSON.
3. Apply `"logic": "AND" | "OR"` correctly.
4. Always expand codes into `IN [...]` lists when multiple are available.
5. Map coding systems to URIs:
   - "SNOMED CT" → `http://snomed.info/sct`
   - "ICD-10-CM" → `http://hl7.org/fhir/sid/icd-10-cm`
   - "RxNorm" → `http://www.nlm.nih.gov/research/umls/rxnorm`
   - "LOINC" → `http://loinc.org`
6. Temporal constraints (like `"daysBefore"`) must filter on `effectiveDateTime`, `onsetDateTime`, or `authoredOn`.
7. Return only distinct patients: `RETURN DISTINCT p LIMIT N`.
---
### INPUT JSON
{json.dumps(criteria_json, indent=2)}

---
### OUTPUT
Only output the Cypher query. No explanations, no extra text.
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0
        )

        query = response.choices[0].message.content.strip()

        # Remove Markdown ```cypher blocks if present
        if query.startswith("```"):
            query = re.sub(r"^```[a-zA-Z0-9]*\n?", "", query)
            query = re.sub(r"\n?```$", "", query)

        return query

    except Exception as e:
        raise ValueError(f"LLM Cypher generation failed: {str(e)}")
