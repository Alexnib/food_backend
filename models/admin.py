from pydantic import BaseModel


class BlockStatusUpdate(BaseModel):
    is_blocked: bool
