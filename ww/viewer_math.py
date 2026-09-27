"""Vector and projection helpers shared by 3D viewer scripts."""

from __future__ import annotations

import math


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def normalize(value):
    length = math.sqrt(dot(value, value)) or 1.0
    return (value[0] / length, value[1] / length, value[2] / length)


def add_scaled(origin, vector, scale):
    return tuple(origin[index] + vector[index] * scale for index in range(3))


def forward_from_angles(azimuth, elevation):
    azimuth = math.radians(azimuth)
    elevation = math.radians(max(-85.0, min(85.0, elevation)))
    return (-math.cos(elevation) * math.sin(azimuth),
            -math.sin(elevation),
            -math.cos(elevation) * math.cos(azimuth))


def angles_from_forward(forward):
    forward = normalize(forward)
    return (math.degrees(math.atan2(-forward[0], -forward[2])),
            math.degrees(math.asin(max(-1.0, min(1.0, -forward[1])))))


class ViewerCamera:
    near = 5.0

    def __init__(self, position, forward, up=None, *, width, height, focal):
        self.position = tuple(position)
        self.forward = normalize(forward)
        basis_up = up if up is not None and dot(up, up) > 0.000001 else (0.0, 1.0, 0.0)
        self.right = normalize(cross(self.forward, basis_up))
        self.up = normalize(cross(self.right, self.forward))
        self.width = float(width)
        self.height = float(height)
        self.focal = float(focal)

    def project(self, point):
        relative = sub(point, self.position)
        depth = dot(relative, self.forward)
        if depth <= self.near:
            return None
        return ((self.width * 0.5 + self.focal * dot(relative, self.right) / depth,
                 self.height * 0.5 - self.focal * dot(relative, self.up) / depth),
                depth)


def screen_ray(camera, screen_point):
    return normalize(tuple(
        camera.forward[index] +
        camera.right[index] * (screen_point[0] - camera.width * 0.5) / camera.focal -
        camera.up[index] * (screen_point[1] - camera.height * 0.5) / camera.focal
        for index in range(3)))
