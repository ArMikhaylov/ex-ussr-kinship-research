#!/usr/bin/env python3
"""Audit cross-references and corroboration claims without assigning truth."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

REQUIRED_FILES = {
    "artifact-register.csv": {
        "artifact_id",
        "independence_group",
        "directness",
        "originality",
        "related_claim_ids",
        "status",
    },
    "claims-register.csv": {
        "claim_id",
        "artifact_ids",
        "supporting_independence_groups",
        "contradicting_artifact_ids",
        "status",
        "rationale",
    },
    "relationship-register.csv": {
        "relationship_id",
        "person_a_id",
        "person_b_id",
        "claim_ids",
        "supporting_artifact_ids",
        "contradicting_artifact_ids",
        "independence_groups",
        "status",
        "rationale",
    },
    "person-register.csv": {"person_id"},
}

CORROBORATED = {"corroborated", "strongly-corroborated"}
VALID_CLAIM_STATUSES = {
    "",
    "hypothesis",
    "single-source",
    "corroborated",
    "strongly-corroborated",
    "contradicted",
    "unresolved",
}


EXTRA_FIELDS_KEY = "__extra_fields__"
PROVENANCED_ORIGINALITY = {"original", "image-of-original", "official-extract"}


def artifact_groups(artifact_ids: Iterable[str], artifact_by_id: dict[str, dict[str, str]]) -> set[str]:
    """Independence groups backed by existing artifacts; declared groups are not counted."""
    return {
        artifact_by_id[artifact_id].get("independence_group", "")
        for artifact_id in artifact_ids
        if artifact_id in artifact_by_id
        and artifact_by_id[artifact_id].get("independence_group", "")
    }


def split_ids(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace(",", ";")
    return [part.strip() for part in normalized.split(";") if part.strip()]


def read_csv(path: Path, required: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, restkey=EXTRA_FIELDS_KEY)
            if reader.fieldnames is None:
                return [], [f"{path.name}: отсутствует строка заголовка"]
            missing = sorted(required - set(reader.fieldnames))
            if missing:
                errors.append(f"{path.name}: отсутствуют колонки: {', '.join(missing)}")
            rows: list[dict[str, str]] = []
            for row in reader:
                extra = row.pop(EXTRA_FIELDS_KEY, None)
                if extra:
                    errors.append(
                        f"{path.name}: строка {reader.line_num}: лишние поля ({len(extra)}) "
                        "сверх заголовка; проверьте разделители и кавычки"
                    )
                rows.append({k: (v or "").strip() for k, v in row.items()})
            return rows, errors
    except OSError as exc:
        return [], [f"{path.name}: ошибка чтения: {exc}"]


def duplicate_ids(rows: Iterable[dict[str, str]], field: str) -> list[str]:
    ids = [row.get(field, "") for row in rows if row.get(field, "")]
    return sorted(key for key, count in Counter(ids).items() if count > 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверяет целостность реестров и минимальные условия кроссподтверждения."
    )
    parser.add_argument("--case-dir", required=True, help="Директория дела")
    parser.add_argument("--json", action="store_true", help="Вывести JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Возвращать ненулевой код также при предупреждениях",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_dir = Path(args.case_dir).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not case_dir.is_dir():
        print(f"Ошибка: директория дела не найдена: {case_dir}", file=sys.stderr)
        return 2

    data: dict[str, list[dict[str, str]]] = {}
    for filename, required in REQUIRED_FILES.items():
        path = case_dir / filename
        if not path.is_file():
            errors.append(f"Отсутствует обязательный файл: {filename}")
            data[filename] = []
            continue
        rows, file_errors = read_csv(path, required)
        errors.extend(file_errors)
        data[filename] = rows

    artifacts = data["artifact-register.csv"]
    claims = data["claims-register.csv"]
    relationships = data["relationship-register.csv"]
    persons = data["person-register.csv"]

    for rows, field, label in [
        (artifacts, "artifact_id", "артефакта"),
        (claims, "claim_id", "утверждения"),
        (relationships, "relationship_id", "связи"),
        (persons, "person_id", "лица"),
    ]:
        duplicates = duplicate_ids(rows, field)
        if duplicates:
            errors.append(f"Дублирующиеся ID {label}: {', '.join(duplicates)}")
        for index, row in enumerate(rows, start=2):
            if not row.get(field, ""):
                warnings.append(f"{field}: пустой ID в строке {index}")

    artifact_by_id = {row["artifact_id"]: row for row in artifacts if row.get("artifact_id")}
    claim_by_id = {row["claim_id"]: row for row in claims if row.get("claim_id")}
    person_ids = {row["person_id"] for row in persons if row.get("person_id")}

    for artifact_id, row in artifact_by_id.items():
        if not row.get("independence_group"):
            warnings.append(f"{artifact_id}: не указан independence_group")
        for claim_id in split_ids(row.get("related_claim_ids", "")):
            if claim_id not in claim_by_id:
                errors.append(f"{artifact_id}: неизвестный related claim_id {claim_id}")

    for claim_id, row in claim_by_id.items():
        status = row.get("status", "")
        if status not in VALID_CLAIM_STATUSES:
            warnings.append(f"{claim_id}: неизвестный статус {status!r}")
        referenced = split_ids(row.get("artifact_ids", ""))
        contradictory = split_ids(row.get("contradicting_artifact_ids", ""))
        for artifact_id in referenced + contradictory:
            if artifact_id not in artifact_by_id:
                errors.append(f"{claim_id}: неизвестный artifact_id {artifact_id}")

        groups = artifact_groups(referenced, artifact_by_id)
        groups_declared = set(split_ids(row.get("supporting_independence_groups", "")))
        unbacked = sorted(groups_declared - groups)
        if unbacked:
            warnings.append(
                f"{claim_id}: группы объявлены без артефактов в artifact_ids: {', '.join(unbacked)}"
            )

        if status in CORROBORATED and len(groups) < 2:
            warnings.append(
                f"{claim_id}: статус {status}, но найдено независимых групп: {len(groups)}"
            )
        if status in CORROBORATED and not row.get("rationale"):
            warnings.append(f"{claim_id}: для {status} отсутствует rationale")
        if status == "single-source" and len(groups) > 1:
            warnings.append(
                f"{claim_id}: несколько независимых групп, но статус всё ещё single-source; проверьте конфликт/полноту"
            )
        if status == "strongly-corroborated":
            known = [artifact_by_id[a] for a in referenced if a in artifact_by_id]
            is_direct = [a.get("directness", "").lower() == "direct" for a in known]
            has_provenance = [
                a.get("originality", "").lower() in PROVENANCED_ORIGINALITY for a in known
            ]
            # Протокол требует одну запись, которая одновременно прямая и с установленным происхождением.
            if not any(d and p for d, p in zip(is_direct, has_provenance)):
                if not any(is_direct):
                    warnings.append(f"{claim_id}: strongly-corroborated без прямого артефакта")
                if not any(has_provenance):
                    warnings.append(
                        f"{claim_id}: strongly-corroborated без оригинала, образа оригинала или официальной выписки"
                    )
                if any(is_direct) and any(has_provenance):
                    warnings.append(
                        f"{claim_id}: strongly-corroborated: прямота и происхождение есть только у разных "
                        "артефактов; нужна одна прямая запись с установленным происхождением"
                    )

    for row in relationships:
        relation_id = row.get("relationship_id", "<без ID>")
        for person_field in ("person_a_id", "person_b_id"):
            person_id = row.get(person_field, "")
            if person_id and person_id not in person_ids:
                errors.append(f"{relation_id}: неизвестный {person_field} {person_id}")
        for claim_id in split_ids(row.get("claim_ids", "")):
            if claim_id not in claim_by_id:
                errors.append(f"{relation_id}: неизвестный claim_id {claim_id}")
        artifact_refs = split_ids(row.get("supporting_artifact_ids", "")) + split_ids(
            row.get("contradicting_artifact_ids", "")
        )
        for artifact_id in artifact_refs:
            if artifact_id not in artifact_by_id:
                errors.append(f"{relation_id}: неизвестный artifact_id {artifact_id}")
        groups = artifact_groups(split_ids(row.get("supporting_artifact_ids", "")), artifact_by_id)
        unbacked = sorted(set(split_ids(row.get("independence_groups", ""))) - groups)
        if unbacked:
            warnings.append(
                f"{relation_id}: группы объявлены без артефактов в supporting_artifact_ids: "
                f"{', '.join(unbacked)}"
            )
        if row.get("status", "") in CORROBORATED:
            if len(groups) < 2:
                warnings.append(
                    f"{relation_id}: статус {row.get('status')}, но указано независимых групп: {len(groups)}"
                )
            if not row.get("rationale"):
                warnings.append(f"{relation_id}: отсутствует rationale")

    result = {
        "case_dir": str(case_dir),
        "counts": {
            "persons": len(persons),
            "artifacts": len(artifacts),
            "claims": len(claims),
            "relationships": len(relationships),
        },
        "errors": errors,
        "warnings": warnings,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Проверено дело: {case_dir}")
        print(
            "Записей: "
            f"лица={len(persons)}, артефакты={len(artifacts)}, "
            f"утверждения={len(claims)}, связи={len(relationships)}"
        )
        if errors:
            print("\nОШИБКИ:")
            for item in errors:
                print(f"- {item}")
        if warnings:
            print("\nПРЕДУПРЕЖДЕНИЯ:")
            for item in warnings:
                print(f"- {item}")
        if not errors and not warnings:
            print("Ошибок и предупреждений не найдено.")

    if errors:
        return 1
    if warnings and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
