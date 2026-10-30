// A textured unit cube as a binary glTF (GLB), built in code so no binary sits in the repo. The
// texture is a 2x2 PNG embedded in the GLB, so the viewer must decode an image to show the cube.
import { deflateSync, crc32 } from "node:zlib";

function chunk(type: string, data: Buffer): Buffer {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(
    crc32(Buffer.concat([Buffer.from(type, "ascii"), data])) >>> 0,
    0,
  );
  return Buffer.concat([head, data, crc]);
}

/** A 2x2 PNG: a teal, white, orange, dark grid. */
function texturePng(): Buffer {
  const rows = [
    [0x1f, 0x9e, 0x9a, 0xff, 0xff, 0xff],
    [0xf2, 0x8c, 0x28, 0x22, 0x22, 0x22],
  ];
  const raw = Buffer.concat(
    rows.map((row) => Buffer.concat([Buffer.from([0]), Buffer.from(row)])),
  );
  const header = Buffer.alloc(13);
  header.writeUInt32BE(2, 0);
  header.writeUInt32BE(2, 4);
  header[8] = 8; // bit depth
  header[9] = 2; // RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const pad4 = (buffer: Buffer) =>
  Buffer.concat([buffer, Buffer.alloc((4 - (buffer.length % 4)) % 4)]);

/** The cube's faces: four corners each, with normal and UV, and two triangles per face. */
function cubeGeometry() {
  const faces = [
    {
      normal: [0, 0, 1],
      corners: [
        [-1, -1, 1],
        [1, -1, 1],
        [1, 1, 1],
        [-1, 1, 1],
      ],
    },
    {
      normal: [0, 0, -1],
      corners: [
        [1, -1, -1],
        [-1, -1, -1],
        [-1, 1, -1],
        [1, 1, -1],
      ],
    },
    {
      normal: [1, 0, 0],
      corners: [
        [1, -1, 1],
        [1, -1, -1],
        [1, 1, -1],
        [1, 1, 1],
      ],
    },
    {
      normal: [-1, 0, 0],
      corners: [
        [-1, -1, -1],
        [-1, -1, 1],
        [-1, 1, 1],
        [-1, 1, -1],
      ],
    },
    {
      normal: [0, 1, 0],
      corners: [
        [-1, 1, 1],
        [1, 1, 1],
        [1, 1, -1],
        [-1, 1, -1],
      ],
    },
    {
      normal: [0, -1, 0],
      corners: [
        [-1, -1, -1],
        [1, -1, -1],
        [1, -1, 1],
        [-1, -1, 1],
      ],
    },
  ];
  const uvs = [
    [0, 1],
    [1, 1],
    [1, 0],
    [0, 0],
  ];
  const positions: number[] = [];
  const normals: number[] = [];
  const texcoords: number[] = [];
  const indices: number[] = [];
  faces.forEach((face, index) => {
    face.corners.forEach((corner, corner_index) => {
      positions.push(...corner);
      normals.push(...face.normal);
      texcoords.push(...uvs[corner_index]);
    });
    const base = index * 4;
    indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
  });
  return { positions, normals, texcoords, indices };
}

export function cubeGlb(): Buffer {
  const { positions, normals, texcoords, indices } = cubeGeometry();
  const positionBytes = Buffer.from(new Float32Array(positions).buffer);
  const normalBytes = Buffer.from(new Float32Array(normals).buffer);
  const texcoordBytes = Buffer.from(new Float32Array(texcoords).buffer);
  const indexBytes = pad4(Buffer.from(new Uint16Array(indices).buffer));
  const image = pad4(texturePng());
  const views = [positionBytes, normalBytes, texcoordBytes, indexBytes, image];
  const offsets: number[] = [];
  let cursor = 0;
  for (const view of views) {
    offsets.push(cursor);
    cursor += view.length;
  }
  const bin = Buffer.concat(views);
  const json = {
    asset: { version: "2.0", generator: "azizam e2e cube" },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ mesh: 0 }],
    meshes: [
      {
        primitives: [
          {
            attributes: { POSITION: 0, NORMAL: 1, TEXCOORD_0: 2 },
            indices: 3,
            material: 0,
          },
        ],
      },
    ],
    materials: [
      {
        pbrMetallicRoughness: {
          baseColorTexture: { index: 0 },
          metallicFactor: 0,
          roughnessFactor: 0.8,
        },
      },
    ],
    textures: [{ source: 0 }],
    images: [{ bufferView: 4, mimeType: "image/png" }],
    accessors: [
      {
        bufferView: 0,
        componentType: 5126,
        count: positions.length / 3,
        type: "VEC3",
        min: [-1, -1, -1],
        max: [1, 1, 1],
      },
      {
        bufferView: 1,
        componentType: 5126,
        count: normals.length / 3,
        type: "VEC3",
      },
      {
        bufferView: 2,
        componentType: 5126,
        count: texcoords.length / 2,
        type: "VEC2",
      },
      {
        bufferView: 3,
        componentType: 5123,
        count: indices.length,
        type: "SCALAR",
      },
    ],
    bufferViews: [
      {
        buffer: 0,
        byteOffset: offsets[0],
        byteLength: positionBytes.length,
        target: 34962,
      },
      {
        buffer: 0,
        byteOffset: offsets[1],
        byteLength: normalBytes.length,
        target: 34962,
      },
      {
        buffer: 0,
        byteOffset: offsets[2],
        byteLength: texcoordBytes.length,
        target: 34962,
      },
      {
        buffer: 0,
        byteOffset: offsets[3],
        byteLength: indexBytes.length,
        target: 34963,
      },
      { buffer: 0, byteOffset: offsets[4], byteLength: image.length },
    ],
    buffers: [{ byteLength: bin.length }],
  };
  // The JSON chunk pads with spaces, as the glTF spec requires; the binary chunk pads with zeros.
  const jsonText = Buffer.from(JSON.stringify(json), "utf8");
  const jsonBytes = Buffer.concat([
    jsonText,
    Buffer.alloc((4 - (jsonText.length % 4)) % 4, 0x20),
  ]);
  const jsonChunk = Buffer.alloc(8 + jsonBytes.length);
  jsonChunk.writeUInt32LE(jsonBytes.length, 0);
  jsonChunk.writeUInt32LE(0x4e4f534a, 4); // "JSON"
  jsonBytes.copy(jsonChunk, 8);
  const binChunk = Buffer.alloc(8 + bin.length);
  binChunk.writeUInt32LE(bin.length, 0);
  binChunk.writeUInt32LE(0x004e4942, 4); // "BIN\0"
  bin.copy(binChunk, 8);
  const header = Buffer.alloc(12);
  header.writeUInt32LE(0x46546c67, 0); // "glTF"
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + jsonChunk.length + binChunk.length, 8);
  return Buffer.concat([header, jsonChunk, binChunk]);
}
