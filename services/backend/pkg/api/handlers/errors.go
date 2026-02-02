package handlers

import (
	"net/http"

	"github.com/go-chi/render"
)

// ErrResponse is a generic error response structure
type ErrResponse struct {
	Err            error  `json:"-"`
	HTTPStatusCode int    `json:"-"`
	Status         string `json:"status"`
	Error          string `json:"error,omitempty"`
}

// Render sets the HTTP status code for the error response
func (e *ErrResponse) Render(w http.ResponseWriter, r *http.Request) error {
	render.Status(r, e.HTTPStatusCode)
	return nil
}

// ErrInvalidRequest returns a 400 Bad Request error
func ErrInvalidRequest(err error) render.Renderer {
	return &ErrResponse{
		Err:            err,
		HTTPStatusCode: http.StatusBadRequest,
		Status:         "error",
		Error:          err.Error(),
	}
}

// ErrUnauthorized returns a 401 Unauthorized error
func ErrUnauthorized() render.Renderer {
	return &ErrResponse{
		HTTPStatusCode: http.StatusUnauthorized,
		Status:         "error",
		Error:          "Unauthorized",
	}
}

// ErrForbidden returns a 403 Forbidden error
func ErrForbidden() render.Renderer {
	return &ErrResponse{
		HTTPStatusCode: http.StatusForbidden,
		Status:         "error",
		Error:          "Forbidden",
	}
}

// ErrNotFound returns a 404 Not Found error
func ErrNotFound() render.Renderer {
	return &ErrResponse{
		HTTPStatusCode: http.StatusNotFound,
		Status:         "error",
		Error:          "Not found",
	}
}

// ErrInternal returns a 500 Internal Server Error
func ErrInternal(err error) render.Renderer {
	return &ErrResponse{
		Err:            err,
		HTTPStatusCode: http.StatusInternalServerError,
		Status:         "error",
		Error:          "Internal server error",
	}
}

// ErrServiceUnavailable returns a 503 Service Unavailable error
func ErrServiceUnavailable(err error) render.Renderer {
	return &ErrResponse{
		Err:            err,
		HTTPStatusCode: http.StatusServiceUnavailable,
		Status:         "error",
		Error:          "Service unavailable",
	}
}
