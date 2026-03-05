cmake_minimum_required(VERSION 3.13)
project(include-tracker)

set (CMAKE_CXX_STANDARD 17)

find_package(LLVM REQUIRED CONFIG)
find_package(Clang REQUIRED CONFIG)

include_directories(SYSTEM ${LLVM_INCLUDE_DIRS} ${CLANG_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})

add_executable(include-tracker main.cpp)

if (APPLE)
  target_link_libraries(include-tracker
    PRIVATE
    clangTooling
    clangBasic
    clangFrontend
    clangLex
    clangAST
  )
else()
  target_link_libraries(include-tracker
    PRIVATE
    clang-cpp
    LLVM
  )
endif()
