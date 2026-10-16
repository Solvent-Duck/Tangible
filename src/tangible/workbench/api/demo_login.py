"""The shared demo login (see `tangible.workbench.demo_login`): 404 unless it is configured."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from tangible.workbench.demo_login import DemoLogin
from tangible.workbench.ratelimit import public_limit

router = APIRouter(prefix="/api")


class Credentials(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=200)


@router.post("/demo-login", dependencies=[Depends(public_limit)], include_in_schema=False)
def demo_login(body: Credentials, request: Request) -> dict[str, Any]:
    demo: DemoLogin | None = request.app.state.demo_login
    if demo is None:
        raise HTTPException(404, "Not Found")
    if not demo.check(body.username, body.password):
        raise HTTPException(401, "wrong username or password")
    token, exp = demo.issue()
    return {"token": token, "expires_at": exp}
