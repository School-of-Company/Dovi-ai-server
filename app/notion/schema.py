from pydantic import BaseModel


class ApiSpecEntry(BaseModel):
    method: str
    path: str
    summary: str
    request_schema: str = ""
    response_schema: str = ""
    auth: str = ""

    def to_text(self) -> str:
        lines = [f"{self.method} {self.path}", self.summary]
        if self.request_schema:
            lines.append(f"Request: {self.request_schema}")
        if self.response_schema:
            lines.append(f"Response: {self.response_schema}")
        if self.auth:
            lines.append(f"Auth: {self.auth}")
        return "\n".join(lines)
