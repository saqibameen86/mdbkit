"""mongosh export scripts, printed by `mdbkit export-script`.

mdbkit never connects to a database. Instead it prints small mongosh
scripts the operator runs themselves, producing JSON files that can be
fed back via --schema / --indexes. The operator sees exactly what runs.
"""

SCHEMA_SCRIPT = r"""// mdbkit schema export -- run with:
//   mongosh --quiet "mongodb://HOST/DB" this_file.js > schema.json
// Samples documents per collection to learn field paths and types.
// Reads only; writes nothing; literal values are NOT exported (types only).
const SAMPLE = 100;      // docs sampled per collection
const MAX_DEPTH = 3;     // nested path depth
const out = { db: db.getName(), generatedAt: new Date().toISOString(),
              sampleSize: SAMPLE, collections: {} };

function typeName(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (v instanceof Date) return "date";
  if (v && v._bsontype === "ObjectId") return "objectId";
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

db.getCollectionNames().filter(c => !c.startsWith("system.")).forEach(coll => {
  const fields = {};
  let n = 0;
  db.getCollection(coll).aggregate([{ $sample: { size: SAMPLE } }]).forEach(doc => {
    n += 1;
    for (const k of Object.keys(doc)) record(fields, k, doc[k], 1);
  });
  const serialized = {};
  for (const [path, info] of Object.entries(fields)) {
    serialized[path] = { types: Array.from(info.types).sort(),
                         presence: n ? Math.round(100 * info.count / n) / 100 : 0 };
  }
  out.collections[coll] = { sampleSize: n, fields: serialized };
});

print(JSON.stringify(out));
"""

INDEXES_SCRIPT = r"""// mdbkit index export -- run with:
//   mongosh --quiet "mongodb://HOST/DB" this_file.js > indexes.json
// Exports index metadata only (getIndexes). Reads nothing else.
const out = { db: db.getName(), generatedAt: new Date().toISOString(),
              collections: {} };
db.getCollectionNames().filter(c => !c.startsWith("system.")).forEach(coll => {
  out.collections[coll] = db.getCollection(coll).getIndexes();
});
print(JSON.stringify(out));
"""
