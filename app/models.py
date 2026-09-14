from pydantic import BaseModel


class Task(BaseModel):
    id: str = ""
    title: str = ""
    status: int = 0
    priority: int = 0
    dueDate: str = ""
    startDate: str = ""
    completedTime: str = ""
    desc: str = ""
    projectId: str = ""
    projectName: str = ""


class SyncRequest(BaseModel):
    date: str  # YYYY-MM-DD


class SyncResult(BaseModel):
    date: str
    page_id: str
    page_url: str
    tasks_synced: int
    created: bool


class TasksResponse(BaseModel):
    date: str
    tasks: list[Task]
    total: int
