"""mongosh export scripts, printed by `mdbkit export-script`.

mdbkit never connects to a database. Instead it prints small mongosh
scripts the operator runs themselves, producing JSON files that can be
fed back via --schema / --indexes. The operator sees exactly what runs.
"""

SCHEMA_SCRIPT = r"""// mdbkit schema export -- run with:
//   mongosh --quiet "mongodb://HOST:PORT/" --eval "$(cat this_file.js)" > schema.json
// Samples documents per collection, in every database you can read
// (admin/config/local skipped), to learn field paths and types.
// Reads only; writes nothing; literal values are NOT exported (types only).
const ONLY_DB = null;    // e.g. "shop" to export a single database
const SAMPLE = 100;      // docs sampled per collection
const MAX_DEPTH = 3;     // nested path depth
// Collections are keyed by full namespace ("shop.orders"), so "db" is empty.
const out = { db: "", generatedAt: new Date().toISOString(),
              sampleSize: SAMPLE, databases: [], collections: {} };

function databaseNames() {
  if (ONLY_DB) return [ONLY_DB];
  const res = db.adminCommand({ listDatabases: 1, nameOnly: true,
                                authorizedDatabases: true });
  return res.databases.map(d => d.name)
            .filter(n => !["admin", "config", "local"].includes(n));
}

function typeName(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (v instanceof Date) return "date";
  if (v && v._bsontype) {
    // BSON wrapper types (Long, Decimal128, ...) are objects in mongosh;
    // name the BSON type rather than walking into {low, high}.
    const names = { ObjectId: "objectId", Long: "long", Int32: "int",
                    Double: "double", Decimal128: "decimal", Binary: "binData",
                    UUID: "binData", Timestamp: "timestamp" };
    return names[v._bsontype] || String(v._bsontype).toLowerCase();
  }
  if (typeof v === "object") return "object";
  if (typeof v === "number") return Number.isInteger(v) ? "int" : "double";
  return typeof v; // string, boolean -> bool below
}

function record(fields, path, v, depth) {
  let t = typeName(v);
  if (t === "boolean") t = "bool";
  if (!fields[path]) fields[path] = { types: new Set(), count: 0 };
  fields[path].types.add(t);
  fields[path].count += 1;
  if (depth >= MAX_DEPTH) return;
  if (t === "object") {
    for (const k of Object.keys(v)) record(fields, path + "." + k, v[k], depth + 1);
  } else if (t === "array" && v.length > 0 && typeof v[0] === "object"
             && v[0] !== null && !Array.isArray(v[0])) {
    for (const k of Object.keys(v[0])) record(fields, path + "." + k, v[0][k], depth + 1);
  }
}

databaseNames().forEach(name => {
  const d = db.getSiblingDB(name);
  out.databases.push(name);
  d.getCollectionInfos({ type: "collection" }).map(c => c.name)
   .filter(c => !c.startsWith("system.")).forEach(coll => {
    const fields = {};
    let n = 0;
    try {
      d.getCollection(coll).aggregate([{ $sample: { size: SAMPLE } }]).forEach(doc => {
        n += 1;
        for (const k of Object.keys(doc)) record(fields, k, doc[k], 1);
      });
    } catch (e) { return; }
    const serialized = {};
    for (const [path, info] of Object.entries(fields)) {
      serialized[path] = { types: Array.from(info.types).sort(),
                           presence: n ? Math.round(100 * info.count / n) / 100 : 0 };
    }
    out.collections[name + "." + coll] = { sampleSize: n, fields: serialized };
  });
});

print(EJSON.stringify(out, { relaxed: true }));
"""

INDEXES_SCRIPT = r"""// mdbkit index export -- run with:
//   mongosh --quiet "mongodb://HOST:PORT/" --eval "$(cat this_file.js)" > indexes.json
// Exports index definitions (getIndexes) and, where your role allows it,
// how often each index has been used ($indexStats), for every database you
// can read (admin/config/local skipped), plus the shard keys from
// config.collections (through mongos) or config.cache.collections (on a
// shard). Reads no documents.
// Usage counters are kept per member and reset when a member restarts:
// run this on each member, or through mongos for a sharded cluster.
const ONLY_DB = null;    // e.g. "shop" to export a single database
// Collections are keyed by full namespace ("shop.orders"), so "db" is empty.
let hello;
try { hello = db.hello(); } catch (e) { hello = db.isMaster(); }  // 4.4.0-4.4.1
const out = { db: "", generatedAt: new Date().toISOString(),
              source: { me: hello.me || null, setName: hello.setName || null,
                        isPrimary: !!(hello.isWritablePrimary || hello.ismaster),
                        isMongos: hello.msg === "isdbgrid" },
              databases: [], collections: {}, usage: {}, shardKeys: {} };
const names = ONLY_DB ? [ONLY_DB] :
  db.adminCommand({ listDatabases: 1, nameOnly: true, authorizedDatabases: true })
    .databases.map(d => d.name)
    .filter(n => !["admin", "config", "local"].includes(n));
names.forEach(name => {
  const d = db.getSiblingDB(name);
  out.databases.push(name);
  d.getCollectionInfos({ type: "collection" }).map(c => c.name)
   .filter(c => !c.startsWith("system.")).forEach(coll => {
    const ns = name + "." + coll;
    try { out.collections[ns] = d.getCollection(coll).getIndexes(); }
    catch (e) { return; /* no privilege on this collection */ }
    try {
      out.usage[ns] = d.getCollection(coll).aggregate([{ $indexStats: {} }]).toArray()
        .map(u => ({ name: u.name, host: u.host, shard: u.shard || null,
                     ops: u.accesses.ops, since: u.accesses.since }));
    } catch (e) { /* $indexStats needs the indexStats privilege */ }
  });
});
// Shard keys: config.collections through mongos, or a shard's routing cache.
for (const src of ["collections", "cache.collections"]) {
  try {
    db.getSiblingDB("config").getCollection(src).find({}, { key: 1 })
      .forEach(c => { if (c.key && !out.shardKeys[c._id]) out.shardKeys[c._id] = c.key; });
  } catch (e) { /* not a sharded cluster, or no privilege */ }
}
print(EJSON.stringify(out, { relaxed: true }));
"""


SERVERSTATUS = """// mdbkit export-script serverstatus
// Save the output and analyse it offline:
//   mongosh --quiet --host HOST --port PORT \\
//     --username USER --password PASS --authenticationDatabase admin \\
//     --eval "$(cat export_serverstatus.js)" > status.json
//   mdbkit serverstatus status.json
//
// For true rates instead of lifetime averages, take two dumps a minute
// apart and compare:
//   mdbkit serverstatus before.json --after after.json
// EJSON (relaxed) writes 64-bit counters as plain numbers; plain
// JSON.stringify would write them as {low, high} objects.
EJSON.stringify(db.adminCommand({ serverStatus: 1 }), { relaxed: true });
"""
