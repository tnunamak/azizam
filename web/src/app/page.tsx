export default function Home() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <div className="max-w-md text-center">
        <p className="text-vf-subtle font-mono text-xs tracking-widest uppercase">
          ai gateway
        </p>
        <h1 className="text-vf-text mt-3 font-serif text-3xl">
          A calmer surface is on its way
        </h1>
        <p className="text-vf-muted mt-3 text-sm">
          This is the new UI shell, statically exported and served by the
          gateway under <code className="text-vf-accent font-mono">/ui</code>.
        </p>
      </div>
    </main>
  );
}
