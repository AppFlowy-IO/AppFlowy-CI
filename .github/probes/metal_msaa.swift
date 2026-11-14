// Exercise fresh multisample textures without AppFlowy or simulator state.
// Context: https://github.com/actions/runner-images/issues/14836
import Foundation
import Metal
import Darwin

enum ProbeFailure: Error {
    case unavailable(String)
    case incompleteCommand(String)
    case badReadback(Int, [UInt8])
}

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: metal-control REPORT_PATH\n", stderr)
    exit(1)
}
let reportURL = URL(fileURLWithPath: CommandLine.arguments[1])
let started = ProcessInfo.processInfo.systemUptime
var iterations = 0
var deviceName = "unavailable"

func report(_ success: Bool, _ error: String?) {
    let value: [String: Any] = [
        "success": success,
        "device": deviceName,
        "sample_count": 4,
        "rendered_iterations": iterations,
        "duration_ms": Int((ProcessInfo.processInfo.systemUptime - started) * 1000),
        "errors": error.map { [$0] } ?? [],
    ]
    do {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        try data.write(to: reportURL, options: .atomic)
        print(String(decoding: data, as: UTF8.self))
    } catch {
        fputs("Could not write the rendering report\n", stderr)
        exit(1)
    }
}

do {
    guard let device = MTLCreateSystemDefaultDevice(),
          device.supportsTextureSampleCount(4),
          let queue = device.makeCommandQueue() else {
        throw ProbeFailure.unavailable("No four-sample Metal device/queue")
    }
    deviceName = device.name
    while ProcessInfo.processInfo.systemUptime - started < 90 {
        try autoreleasepool {
            let size = 64
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(
                pixelFormat: .rgba8Unorm, width: size, height: size, mipmapped: false)
            descriptor.textureType = .type2DMultisample
            descriptor.sampleCount = 4
            descriptor.storageMode = .private
            descriptor.usage = .renderTarget
            guard let multisample = device.makeTexture(descriptor: descriptor) else {
                throw ProbeFailure.unavailable("Multisample texture allocation failed")
            }
            descriptor.textureType = .type2D
            descriptor.sampleCount = 1
            // Intel runners need an explicitly synchronized CPU readback;
            // successful submission alone does not prove the pixels rendered.
            guard let resolved = device.makeTexture(descriptor: descriptor),
                  let readback = device.makeBuffer(length: size * size * 4,
                                                   options: .storageModeManaged),
                  let command = queue.makeCommandBuffer() else {
                throw ProbeFailure.unavailable("Resolve texture/command allocation failed")
            }
            let pass = MTLRenderPassDescriptor()
            pass.colorAttachments[0].texture = multisample
            pass.colorAttachments[0].resolveTexture = resolved
            pass.colorAttachments[0].loadAction = .clear
            pass.colorAttachments[0].storeAction = .multisampleResolve
            pass.colorAttachments[0].clearColor = MTLClearColorMake(0, 1, 0, 1)
            guard let encoder = command.makeRenderCommandEncoder(descriptor: pass) else {
                throw ProbeFailure.unavailable("Render encoder allocation failed")
            }
            encoder.endEncoding()
            guard let blit = command.makeBlitCommandEncoder() else {
                throw ProbeFailure.unavailable("Readback encoder allocation failed")
            }
            blit.copy(from: resolved, sourceSlice: 0, sourceLevel: 0,
                      sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0),
                      sourceSize: MTLSize(width: size, height: size, depth: 1),
                      to: readback, destinationOffset: 0,
                      destinationBytesPerRow: size * 4,
                      destinationBytesPerImage: size * size * 4)
            blit.synchronize(resource: readback)
            blit.endEncoding()
            let completed = DispatchSemaphore(value: 0)
            command.addCompletedHandler { _ in completed.signal() }
            command.commit()
            guard completed.wait(timeout: .now() + 2) == .success else {
                throw ProbeFailure.incompleteCommand(
                    "Completion timed out; command status \(command.status.rawValue)")
            }
            guard command.status == .completed, command.error == nil else {
                throw ProbeFailure.incompleteCommand(String(describing: command.error))
            }
            let bytes = UnsafeRawBufferPointer(start: readback.contents(),
                                              count: size * size * 4)
            for pixel in stride(from: 0, to: bytes.count, by: 4) {
                guard bytes[pixel] == 0, bytes[pixel + 1] == 255,
                      bytes[pixel + 2] == 0, bytes[pixel + 3] == 255 else {
                    throw ProbeFailure.badReadback(pixel / 4, Array(bytes[pixel..<(pixel + 4)]))
                }
            }
            iterations += 1
        }
    }
    guard iterations >= 100 else {
        throw ProbeFailure.unavailable("Insufficient completed render iterations")
    }
    report(true, nil)
} catch {
    report(false, String(describing: error))
    exit(1)
}
