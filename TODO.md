Usar full_dataset.csv como base.
Usar nuevo prompt de claudio para generar expected_output con Haiku 3.5.

### Ejemplos golden (50%)
* Solo usar filas en que precision@K es 2/3 o 3/3.
* Expected_output debe ser respuesta correcta que daría Vivi

### Ejemplos sin repuesta (25%)

* Cambiar el user_input de las filas seleccionadas a uno que no pueda responderse con la lista de retrieved_contexts.
* Usar gpt-oss-120b para hacer este cambio.
-Expected_output debe ser mensaje de rechazo de vivi (como salga en el prompt).

### Ejemplos con contexto ruidoso (15%)
* Sólo usar filas en las que el relevance_score para 2/3 contextos es muy bajo (< 0.1)
* Expected_output debe utilizar sólo el contexto relevante.

### Ejemplos conversacionales (10%)
* Antes de hacerlo, hablar con claudio para ver cómo se utiliza el contexto en el agente RAG.
* Ej: Qué pasa si el 1er mensaje es "Qué es DS1" y el segundo "cómo postulo?"
* Expected_output debe tener sentido con la conversación completa.