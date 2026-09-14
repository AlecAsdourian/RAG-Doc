package handlers

import "errors"

// errInvalidQueryCharacters is returned, as a 400, for a query containing a
// C0 control character other than tab, newline or carriage return.
//
// U+0000 is the one that matters. Postgres text cannot hold it, so the RAG
// service's keyword search fails on it. The RAG service now rejects such a
// query with a 422, but this backend maps any RAG error to 503 "Service
// unavailable", so without this check the caller would see an outage that
// no retry fixes. The rest of the C0 range has no place in a query either.
// Tab, newline and carriage return stay allowed because pasted code
// contains them.
//
// The Python service applies the same rule (services/workers/api/models.py).
// The message is fixed and does not echo the query.
var errInvalidQueryCharacters = errors.New("query contains invalid characters")

// validateQueryText enforces the character rule on a search or chat query.
func validateQueryText(query string) error {
	for _, r := range query {
		if r < 0x20 && r != '\t' && r != '\n' && r != '\r' {
			return errInvalidQueryCharacters
		}
	}
	return nil
}
