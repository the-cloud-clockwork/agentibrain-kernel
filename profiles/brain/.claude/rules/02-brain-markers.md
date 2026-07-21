# Markers — What Earns One

Syntax and the four types are in `CLAUDE.md`. This is the editorial standard.

## @lesson

A fix that took real investigation, or a pattern that saves the next person
time. Not the obvious, not the already-documented.

> psycopg2 connections are not thread-safe — replaced with ThreadedConnectionPool.

## @milestone

A meaningful unit of work, complete and validated. Not individual commits, not
intermediate steps.

## @signal

Something needs attention. Pick the severity honestly — an inflated `critical`
costs the whole fleet's attention, a deflated one buries a real fire.

A credential, key, or token exposed anywhere is `nuclear`, `source=security`,
immediately.

## @decision

An architectural choice a future session should not have to relitigate. Record
the trade-off and why the alternative lost — the reasoning is the value, not
the verdict.

## Standard

Specific enough to act on. "Fixed the bug" is worthless; name the cause and the
fix. One insight per marker. Five per session, and a session with two good ones
beats a session with five weak ones.
