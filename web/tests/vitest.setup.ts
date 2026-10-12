import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// The gate runs vitest beside type checks and 12 pytest workers, where one
// page's first render took 1.3 s against findBy*'s 1 s default. A wait stays
// one polled condition inside a bounded timeout (TEST_STRATEGY.md 3.1); this
// only raises the bound, and a passing wait still returns at once.
configure({ asyncUtilTimeout: 5000 });
