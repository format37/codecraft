cmake_minimum_required(VERSION 3.18)

# Build for several GPU generations so the image runs on friends' cards too (Volta .. Hopper).
# Must be set before project(): enabling the CUDA language fixes the default (sm_52) otherwise.
if(NOT DEFINED CMAKE_CUDA_ARCHITECTURES)
    set(CMAKE_CUDA_ARCHITECTURES 70 75 80 86 89 90)
endif()

project(aicup2020 LANGUAGES CXX CUDA)
set(CMAKE_CUDA_STANDARD 17)

file(GLOB HEADERS "*.hpp" "model/*.hpp")
set_source_files_properties(${HEADERS} PROPERTIES HEADER_FILE_ONLY TRUE)
file(GLOB SRC "*.cpp" "model/*.cpp" "*.cu")
add_executable(aicup2020 ${HEADERS} ${SRC})
