import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

function isWithinDirectory(directory, candidate) {
  const relative = path.relative(directory, candidate);
  return relative === "" ||
    (!path.isAbsolute(relative) && relative !== ".." && !relative.startsWith(`..${path.sep}`));
}

function ensureDirectoryComponent(componentPath) {
  try {
    fs.mkdirSync(componentPath);
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
  }

  const stats = fs.lstatSync(componentPath);
  if (stats.isSymbolicLink()) {
    throw new Error(`Refusing output directory with a symbolic link: ${componentPath}`);
  }
  if (!stats.isDirectory()) {
    throw new Error(`Output path component is not a directory: ${componentPath}`);
  }
}

export function ensureOutputDirectory(requestedPath, {
  cwd = process.cwd(),
  allowExternal = false,
} = {}) {
  const canonicalCwd = fs.realpathSync(cwd);
  const resolvedPath = path.resolve(canonicalCwd, requestedPath);

  if (!isWithinDirectory(canonicalCwd, resolvedPath)) {
    if (!allowExternal) {
      throw new Error("External output paths require an explicit --screenshot-dir option.");
    }
    fs.mkdirSync(resolvedPath, { recursive: true });
    const canonicalOutput = fs.realpathSync(resolvedPath);
    if (!fs.statSync(canonicalOutput).isDirectory()) {
      throw new Error(`Output path is not a directory: ${canonicalOutput}`);
    }
    return canonicalOutput;
  }

  const relativePath = path.relative(canonicalCwd, resolvedPath);
  let currentPath = canonicalCwd;
  for (const component of relativePath.split(path.sep).filter(Boolean)) {
    currentPath = path.join(currentPath, component);
    ensureDirectoryComponent(currentPath);
  }

  return currentPath;
}

export function writeArtifactFile(filePath, data, {
  cwd = process.cwd(),
  allowExternal = false,
} = {}) {
  const outputDirectory = ensureOutputDirectory(path.dirname(filePath), {
    cwd,
    allowExternal,
  });
  const finalPath = path.join(outputDirectory, path.basename(filePath));
  const temporaryPath = path.join(
    outputDirectory,
    `.${path.basename(finalPath)}.${randomUUID()}.tmp`
  );
  let descriptor;

  try {
    descriptor = fs.openSync(temporaryPath, "wx");
    fs.writeFileSync(descriptor, data);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporaryPath, finalPath);
  } catch (error) {
    const cleanupErrors = [];
    if (descriptor !== undefined) {
      try {
        fs.closeSync(descriptor);
      } catch (cleanupError) {
        cleanupErrors.push(cleanupError);
      }
    }
    try {
      fs.unlinkSync(temporaryPath);
    } catch (cleanupError) {
      if (cleanupError.code !== "ENOENT") cleanupErrors.push(cleanupError);
    }
    if (cleanupErrors.length) {
      throw new AggregateError([error, ...cleanupErrors], "Artifact write failed and cleanup was incomplete.");
    }
    throw error;
  }
}
