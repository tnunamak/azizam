import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, fixtureStateDir, test } from "./fixtures/isolation";

// A sign-in picture is private to the session that may see it. The server keeps its own copy, but
// the browser must ask every time: after a sign-out, or when another person (or a person with other
// rights) signs in in the same browser, the same URL must not be answered from the browser's cache.
// The fixture gateway answers https://avatars.fixture.example/... from a file, with the URL appended
// to the bytes, so whose picture came back can be read from its tail (scripts/ui/fixture_gateway.py).

function adminDatabase(baseURL: string) {
  return path.join(fixtureStateDir(Number(new URL(baseURL).port)), "admin.db");
}

function sql(db: string, statement: string) {
  execFileSync("sqlite3", ["-cmd", ".timeout 10000", db, statement], { encoding: "utf8" });
}

const pictureUrl = (person: string) => `https://avatars.fixture.example/${person}.png`;

test("the browser never answers a picture request from its cache, across sessions and rights", async ({
  page,
  context,
  baseURL,
  isolated,
}) => {
  const member = await isolated.member({ name: "Avatar member" });
  await page.goto("/");
  const owner = (await (await page.request.get("/me/api/profile/avatar")).json()) as { person_id: string };
  const db = adminDatabase(baseURL!);
  const ids = [owner.person_id, member.userId];
  const choices = ["picture", "picture"];
  ids.forEach((id, index) =>
    sql(
      db,
      `INSERT INTO user_avatars(user_id, choice, picture_url, picture_set_at, updated_at) VALUES('${id}', '${choices[index]}', '${pictureUrl(id)}', 1760000000, 1760000000) ` +
        "ON CONFLICT(user_id) DO UPDATE SET choice=excluded.choice, picture_url=excluded.picture_url, picture_set_at=excluded.picture_set_at;",
    ),
  );
  try {
    const cdp = await context.newCDPSession(page);
    await cdp.send("Network.enable");
    const sent: string[] = [];
    const cached = new Set<string>();
    cdp.on("Network.requestWillBeSent", (event) => {
      if (event.request.url.includes("/avatar/picture")) sent.push(event.requestId);
    });
    cdp.on("Network.requestServedFromCache", (event) => cached.add(event.requestId));

    const get = (url: string) =>
      page.evaluate(async (target) => {
        const response = await fetch(target);
        const bytes = new Uint8Array(await response.arrayBuffer());
        return {
          status: response.status,
          cacheControl: response.headers.get("cache-control"),
          tail: new TextDecoder().decode(bytes.slice(Math.max(0, bytes.length - 120))),
        };
      }, url);
    const asked = async (url: string, expected: { status: number; owner?: string }) => {
      const before = sent.length;
      const answer = await get(url);
      expect(answer.status, `${url} as the current session`).toBe(expected.status);
      expect(answer.cacheControl).toBe("no-store");
      if (expected.owner) expect(answer.tail).toContain(pictureUrl(expected.owner));
      // The server was asked: one request went out, and none was served from the cache.
      expect(sent.length).toBe(before + 1);
      expect([...cached].filter((id) => sent.includes(id))).toEqual([]);
    };

    const own = `/me/api/profile/avatar/picture?v=1760000000`;
    const ownerCookies = await context.cookies();
    const memberCookies = await member.context.cookies();

    // A's session: A's picture.
    await asked(own, { status: 200, owner: owner.person_id });
    // The same URL in B's session answers with B's picture, not A's saved bytes.
    await context.clearCookies();
    await context.addCookies(memberCookies);
    await asked(own, { status: 200, owner: member.userId });
    // Signed out: refused, not answered from the cache.
    await context.clearCookies();
    await asked(own, { status: 401 });
    // A member may not see another person's picture; an owner may, when that person chose it.
    await context.addCookies(memberCookies);
    await asked(`/me/api/profile/avatar/picture?person=${owner.person_id}&v=1760000000`, { status: 404 });
    await context.clearCookies();
    await context.addCookies(ownerCookies);
    await asked(`/me/api/profile/avatar/picture?person=${member.userId}&v=1760000000`, {
      status: 200,
      owner: member.userId,
    });
    // The member's rights change (they stop choosing the picture): the owner's next request is refused.
    sql(db, `UPDATE user_avatars SET choice='auto' WHERE user_id='${member.userId}';`);
    await asked(`/me/api/profile/avatar/picture?person=${member.userId}&v=1760000000`, { status: 404 });
  } finally {
    sql(db, `DELETE FROM user_avatars WHERE user_id IN ('${owner.person_id}', '${member.userId}');`);
  }
});
