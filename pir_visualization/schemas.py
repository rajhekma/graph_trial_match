# app/schemas.py
from pydantic import BaseModel
from typing import List, Optional


class ClusterNodeRequestItem(BaseModel):
    """
    Represents one label node to expand in Neo4j.
    Only needs id + label.
    """
    id: int
    label: str



class ClusterRequest(BaseModel):
    nodes: List[ClusterNodeRequestItem]
    max_hops: Optional[int] = 2
    max_neighbors: Optional[int] = 200
