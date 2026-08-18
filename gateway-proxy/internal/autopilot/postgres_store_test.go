// Module: PostgreSQL experiment-store startup tests.
// System role: protects bounded startup connectivity checks and cleanup.
// Dependencies: Go testing, context, and the pgx row contract.
// Side effects: replaces the package-local pool constructor for each test.
package autopilot

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

type fakePostgresPool struct {
	ping       func(context.Context) error
	pingCalls  int
	closeCalls int
}

func (p *fakePostgresPool) Ping(ctx context.Context) error {
	p.pingCalls++
	if p.ping != nil {
		return p.ping(ctx)
	}
	return nil
}

func (*fakePostgresPool) QueryRow(context.Context, string, ...any) pgx.Row {
	return nil
}

func (p *fakePostgresPool) Close() {
	p.closeCalls++
}

func TestNewPostgresExperimentStorePingsBeforeReturning(t *testing.T) {
	pool := &fakePostgresPool{ping: func(ctx context.Context) error {
		deadline, ok := ctx.Deadline()
		if !ok {
			return errors.New("ping context has no deadline")
		}
		if remaining := time.Until(deadline); remaining <= 0 || remaining > time.Second {
			return fmt.Errorf("unexpected ping deadline remaining: %s", remaining)
		}
		return nil
	}}
	original := openPostgresPool
	openPostgresPool = func(context.Context, string) (postgresPool, error) { return pool, nil }
	t.Cleanup(func() { openPostgresPool = original })

	store, err := NewPostgresExperimentStore(context.Background(), "postgresql://test", time.Second)
	if err != nil {
		t.Fatalf("NewPostgresExperimentStore: %v", err)
	}
	if pool.pingCalls != 1 {
		t.Fatalf("ping calls = %d, want 1", pool.pingCalls)
	}
	store.Close()
	if pool.closeCalls != 1 {
		t.Fatalf("close calls = %d, want 1", pool.closeCalls)
	}
}

func TestNewPostgresExperimentStorePingTimeoutFailsStartupAndClosesPool(t *testing.T) {
	pool := &fakePostgresPool{ping: func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	}}
	original := openPostgresPool
	openPostgresPool = func(context.Context, string) (postgresPool, error) { return pool, nil }
	t.Cleanup(func() { openPostgresPool = original })

	started := time.Now()
	store, err := NewPostgresExperimentStore(context.Background(), "postgresql://test", 10*time.Millisecond)
	if store != nil {
		t.Fatal("store should be nil after ping timeout")
	}
	if err == nil || !strings.Contains(err.Error(), "postgres ping failed") {
		t.Fatalf("error = %v, want postgres ping failure", err)
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("startup ping took %s, want a bounded failure", elapsed)
	}
	if pool.pingCalls != 1 || pool.closeCalls != 1 {
		t.Fatalf("ping calls = %d, close calls = %d; want 1 each", pool.pingCalls, pool.closeCalls)
	}
}
