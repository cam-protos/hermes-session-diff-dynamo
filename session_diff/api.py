"""Read-only routes. Clients address recorded IDs, never arbitrary file paths."""

from fastapi import APIRouter, HTTPException, Query
from .store import Store

router = APIRouter()


@router.get("/sessions")
def sessions():
    return {"sessions": Store().sessions(), "version": "0.1.0"}


@router.get("/changes")
def changes(session: str = Query(min_length=1, max_length=512), run: str = Query(default="latest", max_length=128)):
    return Store().view(session, run)


@router.get("/diff")
def diff(session: str = Query(min_length=1, max_length=512), first: int = Query(ge=1), last: int = Query(ge=1)):
    try:
        return Store().diff(session, first, last)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/history")
def history(session: str = Query(min_length=1, max_length=512), first: int = Query(ge=1), last: int = Query(ge=1)):
    try:
        return {"history": Store().history(session, first, last)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
