"""A model for audit records to point at as their subject."""

from __future__ import annotations

from django.db import models


class Article(models.Model):
    """Something worth auditing. Never referenced by a foreign key from the log.

    That is the point: ``Audit`` names its subject with a type string and a
    primary key as text, so deleting this row leaves the record that describes
    it intact and readable.
    """

    title = models.CharField(max_length=255)

    def __str__(self) -> str:
        return self.title


class Note(models.Model):
    """A second subject type, so filters that select *by type* have something to
    select between."""

    body = models.TextField(blank=True)

    def __str__(self) -> str:
        return self.body[:50]
