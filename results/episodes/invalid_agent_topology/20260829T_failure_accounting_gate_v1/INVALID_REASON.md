This failure-path gate correctly retained three paid MARA calls and their token usage, but
the aggregate writer crashed because the error record omitted the zero-valued `graph_trips`
field. The raw sample and trace receipt are retained unchanged. The replacement gate adds a
complete zero-valued DB metric surface to failed episodes.
