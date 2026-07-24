def clause_search_text(clause: dict, *, key_fields_name: str = "key_fields") -> str:
    key_fields = clause.get(key_fields_name) or {}
    field_values = []
    if isinstance(key_fields, dict):
        for field_name, value in key_fields.items():
            field_values.append(str(field_name))
            if isinstance(value, list):
                field_values.extend(str(item) for item in value)
            elif value is not None:
                field_values.append(str(value))
    return " ".join(
        part
        for part in [
            str(clause.get("title", "")),
            str(clause.get("clause_type", "")),
            str(clause.get("text", "")),
            *field_values,
        ]
        if part.strip()
    )
